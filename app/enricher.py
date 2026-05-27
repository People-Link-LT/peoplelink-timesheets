"""
AI metadata enrichment for FileCatalog and KnowledgeChunk.

FileCatalog: each row gets doc_type, company, doc_number, doc_year, doc_month derived
             from drive name + folder path + filename via GPT-4o-mini.
KnowledgeChunk: SharePoint chunks get ai_summary, ai_topics, ai_applies_to.

Idempotent: skips already-enriched rows unless force=True.
Runs async with a concurrency semaphore to stay within OpenAI rate limits.
"""
import asyncio
import json
import logging
import unicodedata
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import FileCatalog, KnowledgeChunk

logger = logging.getLogger(__name__)

_CONCURRENCY = 20   # parallel OpenAI calls
_BATCH_COMMIT = 100  # rows per DB commit


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_FILE_SYSTEM = """\
You are a document classifier for People Link, a Lithuanian recruitment and HR consulting company.
Given a SharePoint file location (drive library name, folder path, filename), extract structured metadata.

Return ONLY valid JSON — no extra text, no markdown:
{
  "doc_type": "<one of the types below>",
  "company": "<client/partner company name, or null for internal docs>",
  "doc_number": "<serial/reference number as string, or null>",
  "doc_year": <4-digit year as integer, or null>,
  "doc_month": <month 1-12 as integer, or null>
}

Valid doc_type values:
  invoice, proposal, client_contract, supplier_contract, nda, employment_contract,
  freelance_contract, loan_contract, power_of_attorney, order, request, policy,
  instruction, announcement, training, marketing, results, template, gdpr, safety,
  health_insurance, health_check, works_council, it_assets, vehicle, office,
  consulting, assessment, survey, client_growth, debt, other

Drive name → doc_type hints (Lithuanian drive names):
  Sąskaitos → invoice
  Pasiūlymai → proposal
  Sutartys su klientais → client_contract
  Sutartys su tiekėjais → supplier_contract
  Konfidencialumo sutartys → nda
  Darbuotojų darbo sutartys, priedai → employment_contract
  Freelance ir praktikos sutartys → freelance_contract
  Paskolos sutartys → loan_contract
  Įgaliojimai → power_of_attorney
  Įsakymai → order
  Prašymai → request
  Tvarkos → policy
  Instrukcijos → instruction
  Skelbimai / Aktualūs dokumentai → announcement
  Mokymai / Akademija → training
  Logo, marketingas / Komunikacija → marketing
  Rezultatai → results
  Šablonai → template
  GDPR dokumentai darbuotojams / Asmens duomenų apsauga → gdpr
  Darbų sauga → safety
  Sveikatos patikra → health_check
  Sveikatos draudimas → health_insurance
  Darbo taryba → works_council
  Skolininkai → debt
  IT ir mobilūs įrenginiai → it_assets
  Auto ir kuras → vehicle
  Biurai → office
  People Link methods / Consulting / Tripod → consulting
  Tripod methods (surveys) → survey
  Assessment as a Service → assessment
  Klientų auginimas → client_growth
  Kiti dokumentai / Kita → infer from filename; use "other" if unclear

Company extraction:
  For client-facing drives (Sąskaitos, Pasiūlymai, Sutartys su klientais, Sutartys su tiekėjais,
  Konfidencialumo sutartys, Klientų auginimas) the FIRST folder segment in the path IS the company.
  For internal/HR/admin drives — company is null.

Document number: extract from patterns like "Nr. 00678", "Nr.45", "#123", "S Nr.00678".
Date: year and month may appear as "2025-03", "2025 03", "2025 kovo", "2025 m. kovo", etc.
"""

_CHUNK_SYSTEM = """\
Analyze this excerpt from a People Link (Lithuanian HR/recruitment company) internal document.
Return ONLY valid JSON — no markdown, no extra text:
{
  "summary": "<1-2 sentence summary in the same language as the document>",
  "topics": ["<topic1>", "<topic2>", "<topic3>"],
  "applies_to": "<all_employees | managers | freelancers | clients | admin | null>"
}
"""


# ---------------------------------------------------------------------------
# Core async helper
# ---------------------------------------------------------------------------

async def _call_json(
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
    system: str,
    user: str,
    max_tokens: int = 200,
) -> dict | None:
    async with sem:
        try:
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                temperature=0,
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.error(f"OpenAI enrichment call failed: {e}")
            return None


# ---------------------------------------------------------------------------
# FileCatalog enrichment
# ---------------------------------------------------------------------------

async def enrich_catalog(db: Session, force: bool = False) -> int:
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY not set — skipping catalog enrichment")
        return 0

    stmt = select(FileCatalog)
    if not force:
        stmt = stmt.where(FileCatalog.enriched_at.is_(None))
    rows: list[FileCatalog] = list(db.execute(stmt).scalars().all())

    if not rows:
        logger.info("file_catalog: all rows already enriched")
        return 0

    logger.info(f"file_catalog: enriching {len(rows)} rows")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    sem = asyncio.Semaphore(_CONCURRENCY)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async def _process(row: FileCatalog):
        user_msg = (
            f"Drive: {row.drive}\n"
            f"Folder: {row.folder_path or '(root)'}\n"
            f"File: {row.name}"
        )
        result = await _call_json(client, sem, _FILE_SYSTEM, user_msg)
        if not result:
            return
        row.doc_type = (result.get("doc_type") or "other") or None
        company = result.get("company") or None
        row.company = company
        row.company_norm = _normalize(company) if company else None
        doc_num = result.get("doc_number")
        row.doc_number = str(doc_num) if doc_num else None
        doc_year = result.get("doc_year")
        row.doc_year = int(doc_year) if doc_year else None
        doc_month = result.get("doc_month")
        row.doc_month = int(doc_month) if doc_month else None
        row.enriched_at = now

    enriched = 0
    for i in range(0, len(rows), _BATCH_COMMIT):
        batch = rows[i : i + _BATCH_COMMIT]
        await asyncio.gather(*[_process(r) for r in batch])
        db.commit()
        enriched += len(batch)
        logger.info(f"file_catalog enrichment: {enriched}/{len(rows)}")

    return enriched


# ---------------------------------------------------------------------------
# KnowledgeChunk enrichment
# ---------------------------------------------------------------------------

async def enrich_chunks(db: Session, force: bool = False) -> int:
    if not settings.openai_api_key:
        return 0

    stmt = select(KnowledgeChunk).where(KnowledgeChunk.source_type == "sharepoint")
    if not force:
        stmt = stmt.where(KnowledgeChunk.ai_summary.is_(None))
    rows: list[KnowledgeChunk] = list(db.execute(stmt).scalars().all())

    if not rows:
        logger.info("knowledge_chunks: all SharePoint chunks already enriched")
        return 0

    logger.info(f"knowledge_chunks: enriching {len(rows)} chunks")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _process(chunk: KnowledgeChunk):
        result = await _call_json(
            client, sem, _CHUNK_SYSTEM,
            chunk.content[:3000],
            max_tokens=300,
        )
        if not result:
            return
        chunk.ai_summary = result.get("summary") or None
        topics = result.get("topics")
        chunk.ai_topics = json.dumps(topics, ensure_ascii=False) if isinstance(topics, list) else None
        applies = result.get("applies_to")
        chunk.ai_applies_to = applies if applies and applies != "null" else None

    enriched = 0
    for i in range(0, len(rows), _BATCH_COMMIT):
        batch = rows[i : i + _BATCH_COMMIT]
        await asyncio.gather(*[_process(r) for r in batch])
        db.commit()
        enriched += len(batch)
        logger.info(f"chunk enrichment: {enriched}/{len(rows)}")

    return enriched


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run_enrichment(force: bool = False) -> dict:
    db = SessionLocal()
    try:
        cat_count = await enrich_catalog(db, force=force)
        chunk_count = await enrich_chunks(db, force=force)
        msg = f"Enriched {cat_count} catalog files + {chunk_count} knowledge chunks"
        logger.info(msg)
        return {"ok": True, "message": msg}
    except Exception as e:
        db.rollback()
        logger.error(f"Enrichment failed: {e}")
        return {"ok": False, "message": str(e)}
    finally:
        db.close()


def run_enrichment_sync(force: bool = False) -> dict:
    return asyncio.run(_run_enrichment(force=force))
