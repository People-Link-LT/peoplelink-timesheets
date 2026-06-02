"""
AI metadata enrichment for FileCatalog and KnowledgeChunk.

For each file in file_catalog:
  - If the file was content-indexed (has rows in knowledge_chunks), the actual
    document text is sent to GPT-4o-mini together with the SharePoint location.
    The AI extracts doc_type, company, doc_number, doc_year, doc_month, a summary,
    topics, and audience — and both tables are updated from a single API call.
  - If the file was NOT indexed (binary, .xls, unsupported format), only the
    drive name + folder path + filename is sent. Only file_catalog is updated.

Idempotent: skips already-enriched rows unless force=True.
Runs async with a concurrency semaphore to stay within OpenAI rate limits.
"""
import asyncio
import json
import logging
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import FileCatalog, KnowledgeChunk

logger = logging.getLogger(__name__)

_CONCURRENCY = 3     # parallel OpenAI calls — stay well under 200K TPM / 500 RPM
_CALL_SPACING = 1.2  # seconds to hold semaphore after each call (~2.5 req/sec max)
_BATCH_COMMIT = 50   # rows per DB commit
_CONTENT_CHARS = 4000  # max document chars to send per file


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_LOCATION_HINTS = """\
Drive name → doc_type hints (Lithuanian SharePoint library names):
  Sąskaitos → invoice | Pasiūlymai → proposal
  Sutartys su klientais → client_contract | Sutartys su tiekėjais → supplier_contract
  Konfidencialumo sutartys → nda | Darbuotojų darbo sutartys, priedai → employment_contract
  Freelance ir praktikos sutartys → freelance_contract | Paskolos sutartys → loan_contract
  Įgaliojimai → power_of_attorney | Įsakymai → order | Prašymai → request
  Tvarkos → policy | Instrukcijos → instruction
  Skelbimai / Aktualūs dokumentai → announcement | Mokymai / Akademija → training
  Logo, marketingas / Komunikacija → marketing | Rezultatai → results | Šablonai → template
  GDPR dokumentai / Asmens duomenų apsauga → gdpr | Darbų sauga → safety
  Sveikatos patikra → health_check | Sveikatos draudimas → health_insurance
  Darbo taryba → works_council | Skolininkai → debt | IT ir mobilūs įrenginiai → it_assets
  Auto ir kuras → vehicle | Biurai → office | People Link methods / Consulting → consulting
  Tripod methods (surveys) → survey | Assessment as a Service → assessment
  Klientų auginimas → client_growth | Kiti dokumentai / Kita → infer from content or filename

Company: for client-facing drives (Sąskaitos, Pasiūlymai, Sutartys su klientais,
  Sutartys su tiekėjais, Konfidencialumo sutartys, Klientų auginimas) the company
  name is usually the first folder in the path. Confirm or correct from document content.
  For internal/HR/admin drives — company is null unless the document itself names a client."""

_VALID_DOC_TYPES = (
    "invoice, proposal, client_contract, supplier_contract, nda, employment_contract, "
    "freelance_contract, loan_contract, power_of_attorney, order, request, policy, "
    "instruction, announcement, training, marketing, results, template, gdpr, safety, "
    "health_insurance, health_check, works_council, it_assets, vehicle, office, "
    "consulting, assessment, survey, client_growth, debt, other"
)

def _build_system_prompts(valid_types: str) -> tuple[str, str]:
    with_content = f"""\
You are a document classifier for People Link, a Lithuanian recruitment and HR consulting company.
You are given the SharePoint location AND the full text of a document.
Use the document content as the primary source of truth; use the location as supporting context.

Return ONLY valid JSON — no extra text, no markdown fences:
{{
  "doc_type": "<type>",
  "company": "<client/partner company name extracted from the document, or null>",
  "doc_number": "<serial/reference number as string, or null>",
  "doc_year": <4-digit year as integer, or null>,
  "doc_month": <month 1-12 as integer, or null>,
  "summary": "<1-2 sentence summary in the same language as the document>",
  "topics": ["<topic1>", "<topic2>", "<topic3>"],
  "applies_to": "<all_employees | managers | freelancers | clients | admin | null>"
}}

Valid doc_type values: {valid_types}

{_LOCATION_HINTS}

For doc_number: look for patterns like "Nr. 00678", "Nr.45", "Sąskaita Nr.", "Sutarties Nr.", "#123".
For dates: year and month may appear as "2025-03", "2025 kovo", "2025 m. kovo mėn.", "kovo 2025", etc.
For company: read the document header — the client/counterparty company name is usually near the top.
For summary: 1-2 sentences describing what this document is and what it covers.
For applies_to: who this document is relevant to inside People Link.
"""
    no_content = f"""\
You are a document classifier for People Link, a Lithuanian recruitment and HR consulting company.
The document text is not available — classify from the SharePoint location alone.

Return ONLY valid JSON — no extra text, no markdown fences:
{{
  "doc_type": "<type>",
  "company": "<client/partner company name from folder path, or null>",
  "doc_number": "<serial/reference number from filename, or null>",
  "doc_year": <4-digit year as integer, or null>,
  "doc_month": <month 1-12 as integer, or null>
}}

Valid doc_type values: {valid_types}

{_LOCATION_HINTS}

For doc_number: extract from filename patterns like "Nr. 00678", "Nr.45", "S Nr.00678".
For dates: extract from filename if present — "2025-03", "2025 03", year only, etc.
"""
    return with_content, no_content


# ---------------------------------------------------------------------------
# Core async helper
# ---------------------------------------------------------------------------

async def _call_json(
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
    system: str,
    user: str,
    max_tokens: int = 400,
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
            result = json.loads(resp.choices[0].message.content)
            # Hold the slot after a successful call to pace throughput
            await asyncio.sleep(_CALL_SPACING)
            return result
        except Exception as e:
            err = str(e)
            if "429" in err:
                # Rate limited — wait longer before releasing the slot
                wait = 60
                logger.warning(f"OpenAI 429 — sleeping {wait}s before continuing")
                await asyncio.sleep(wait)
            else:
                logger.error(f"OpenAI enrichment call failed: {e}")
            return None


# ---------------------------------------------------------------------------
# Main enrichment pass
# ---------------------------------------------------------------------------

async def enrich_all(db: Session, force: bool = False) -> dict:
    """
    Single pass: enrich file_catalog using document content from knowledge_chunks.
    For each file, content is taken from knowledge_chunks (if indexed), otherwise
    only the SharePoint location is used.
    Returns {"catalog": N, "chunks": M}.
    """
    # Load doc types from DB; fall back to hardcoded list if the table is empty
    from app.models import MetaCriteria as _MetaCriteria
    criteria_rows = db.execute(
        select(_MetaCriteria).where(_MetaCriteria.criteria_type == "doc_type")
    ).scalars().all()
    valid_types = ", ".join(r.value for r in criteria_rows) if criteria_rows else _VALID_DOC_TYPES
    _SYSTEM_WITH_CONTENT, _SYSTEM_NO_CONTENT = _build_system_prompts(valid_types)

    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY not set — skipping enrichment")
        return {"catalog": 0, "chunks": 0}

    # Load no_index exclusions so we skip them entirely
    from app.models import DocMeta as _DocMeta
    no_index_meta = db.execute(select(_DocMeta).where(_DocMeta.no_index == True)).scalars().all()
    no_index_drives: set[str] = set()
    no_index_item_ids: set[str] = set()
    no_index_paths_by_drive: dict[str, set[str]] = {}
    for r in no_index_meta:
        if r.item_id.startswith("cat::") or r.item_id.startswith("subcat::"):
            try:
                from app.indexer import _drive_for_meta_key
                d = _drive_for_meta_key(r.item_id)
                if d:
                    no_index_drives.add(d)
            except Exception:
                pass
        else:
            no_index_item_ids.add(r.item_id)
            if r.drive and r.path:
                no_index_paths_by_drive.setdefault(r.drive, set()).add(r.path)

    def _is_no_index(row: FileCatalog) -> bool:
        if row.drive in no_index_drives:
            return True
        if row.item_id in no_index_item_ids:
            return True
        for p in no_index_paths_by_drive.get(row.drive, set()):
            if row.folder_path == p or row.folder_path.startswith(p + "/"):
                return True
        return False

    # Load file_catalog rows that need enrichment
    stmt = select(FileCatalog)
    if not force:
        stmt = stmt.where(FileCatalog.enriched_at.is_(None))
    catalog_rows: list[FileCatalog] = [r for r in db.execute(stmt).scalars().all() if not _is_no_index(r)]

    if not catalog_rows:
        logger.info("All file_catalog rows already enriched")
        return {"catalog": 0, "chunks": 0}

    logger.info(f"Enriching {len(catalog_rows)} file_catalog rows")

    from app import progress as _prog
    _prog.update("enrichment", running=True, done=0, total=len(catalog_rows))

    # Build a map: item_id → list of KnowledgeChunk (all chunks for that file)
    # source_id is either "{item_id}" (chunk 0) or "{item_id}::N" (chunk N)
    all_chunks: list[KnowledgeChunk] = list(
        db.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.source_type == "sharepoint")
        ).scalars().all()
    )
    chunks_by_item: dict[str, list[KnowledgeChunk]] = defaultdict(list)
    for chunk in all_chunks:
        base_id = chunk.source_id.split("::")[0]
        chunks_by_item[base_id].append(chunk)

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    sem = asyncio.Semaphore(_CONCURRENCY)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    cat_enriched = 0
    chunk_enriched = 0

    async def _process(row: FileCatalog) -> int:
        """Returns number of knowledge_chunks updated (0 or more)."""
        file_chunks = chunks_by_item.get(row.item_id, [])

        location_ctx = (
            f"Drive: {row.drive}\n"
            f"Folder: {row.folder_path or '(root)'}\n"
            f"File: {row.name}"
        )

        if file_chunks:
            # Sort by chunk index so we send the beginning of the document first
            file_chunks_sorted = sorted(
                file_chunks,
                key=lambda c: int(c.source_id.split("::")[-1]) if "::" in c.source_id else 0
            )
            # Concatenate up to _CONTENT_CHARS characters
            combined = "\n\n---\n\n".join(c.content for c in file_chunks_sorted)
            content_snippet = combined[:_CONTENT_CHARS]
            user_msg = f"{location_ctx}\n\nDocument content:\n{content_snippet}"
            result = await _call_json(client, sem, _SYSTEM_WITH_CONTENT, user_msg, max_tokens=400)
        else:
            user_msg = location_ctx
            result = await _call_json(client, sem, _SYSTEM_NO_CONTENT, user_msg, max_tokens=200)

        if not result:
            return 0

        # Update file_catalog
        row.doc_type = result.get("doc_type") or "other"
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

        if not file_chunks:
            return 0

        # Propagate summary/topics/applies_to to all chunks for this file
        summary = result.get("summary") or None
        topics = result.get("topics")
        topics_json = json.dumps(topics, ensure_ascii=False) if isinstance(topics, list) else None
        applies = result.get("applies_to")
        applies_to = applies if applies and applies != "null" else None

        for chunk in file_chunks:
            chunk.ai_summary = summary
            chunk.ai_topics = topics_json
            chunk.ai_applies_to = applies_to

        return len(file_chunks)

    # Process in commit-sized batches
    for i in range(0, len(catalog_rows), _BATCH_COMMIT):
        batch = catalog_rows[i : i + _BATCH_COMMIT]
        results = await asyncio.gather(*[_process(r) for r in batch])
        db.commit()
        cat_enriched += len(batch)
        chunk_enriched += sum(results)
        _prog.update("enrichment", done=cat_enriched)
        logger.info(
            f"Enrichment progress: {cat_enriched}/{len(catalog_rows)} files "
            f"({chunk_enriched} chunks updated)"
        )

    _prog.update("enrichment", running=False)
    logger.info(f"Enrichment complete: {cat_enriched} catalog rows, {chunk_enriched} chunks")
    return {"catalog": cat_enriched, "chunks": chunk_enriched}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run_enrichment(force: bool = False) -> dict:
    db = SessionLocal()
    try:
        result = await enrich_all(db, force=force)
        msg = f"Enriched {result['catalog']} catalog files, updated {result['chunks']} knowledge chunks"
        logger.info(msg)
        return {"ok": True, "message": msg}
    except Exception as e:
        db.rollback()
        logger.error(f"Enrichment failed: {e}")
        from app import progress as _prog
        _prog.update("enrichment", running=False)
        return {"ok": False, "message": str(e)}
    finally:
        db.close()


def run_enrichment_sync(force: bool = False) -> dict:
    return asyncio.run(_run_enrichment(force=force))
