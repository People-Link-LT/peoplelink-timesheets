import asyncio
import json
import logging
import unicodedata
from typing import AsyncGenerator

from openai import AsyncOpenAI
from sqlalchemy import select, and_, or_, nullslast
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, AskRule, FileCatalog

logger = logging.getLogger(__name__)

_SYSTEM_BASE = """You are Ask PL, the internal AI assistant for People Link employees.
Your knowledge comes exclusively from People Link's SharePoint documents. Never use outside knowledge to answer factual questions about the company — if it's not in the documents, say so.
Always respond in the same language the user asked in (Lithuanian if they asked in Lithuanian, English if in English).

## Finding documents
Use the search_files tool whenever the user asks to find or list specific files — invoices (sąskaitos), proposals (pasiūlymai), contracts (sutartys), or any other document type for a company.
- Pass only the company name as `query` — never include document-type words in the query string; use the `doc_type` parameter for filtering instead.
- The catalog has AI-enriched metadata: each file has a `doc_type` (invoice, proposal, client_contract, …) and a `company` field extracted from SharePoint. Search matches against the company name field directly, so short company names work best (e.g. "Ramirent" not "UAB Ramirent AS").
- If a search returns no results, retry with a shorter version of the company name (drop "UAB", "AS", "Ltd", etc.).
- Results are grouped by company with doc_type labels. Present them as a markdown list of clickable links, newest first.
- The result shows company name, doc_type, document number, and date for each file — trust these fields completely.

## Answering from documents
For general knowledge questions (HR policies, processes, growth system, etc.), answer from the context provided below.
Always cite the source: if a URL is available, make it a clickable link — "According to [Document name](URL)…"
When multiple versions of the same document exist, prefer and cite the one with the most recent date.
Be concise and professional. Use bullet points for lists."""

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL_OPENAI = "gpt-4o-mini"
CHAT_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"  # upgrade to claude-sonnet-4-6 when org rate limit is raised
TOP_K = 3           # 10K token/min rate limit: 3 chunks + tool round stays ~8K total
MAX_FILE_RESULTS = 20

# Keyword filters per document type (matched against name_norm — fallback for unenriched rows)
_DOC_TYPE_KEYWORDS = {
    "invoice":  ["saskait", "faktura", "invoice"],
    "proposal": ["pasiulym", "proposal"],
    "contract": ["sutart", "contract"],
}

# Maps the tool's doc_type strings to internal enriched doc_type values
_DOC_TYPE_INTERNAL: dict[str, list[str]] = {
    "invoice":  ["invoice"],
    "proposal": ["proposal"],
    "contract": ["client_contract", "supplier_contract", "nda", "employment_contract",
                 "freelance_contract", "loan_contract"],
}

SEARCH_FILES_TOOL = {
    "name": "search_files",
    "description": (
        "Search the People Link SharePoint document catalog. Each file has AI-enriched metadata: "
        "a doc_type field (invoice, proposal, client_contract, etc.) and a company field extracted "
        "from the document itself. Searches match against the company name field directly — "
        "pass only the company name as query (e.g. 'Ramirent', not 'Ramirent invoices'). "
        "Use doc_type to filter by document type. If query is empty and doc_type is set, "
        "returns all files of that type. Returns up to 20 files with SharePoint links, newest first. "
        "Prefer this tool for any 'find/list all X for company Y' request."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Company name or keywords to match in file names / folder paths, e.g. 'Light Conversion'.",
            },
            "doc_type": {
                "type": "string",
                "enum": ["invoice", "proposal", "contract", "any"],
                "description": "Optional document-type filter. Use 'any' (or omit) when unsure.",
            },
        },
        "required": ["query"],
    },
}


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()


def search_files(db: Session, query: str, doc_type: str | None = None, limit: int = MAX_FILE_RESULTS) -> list[FileCatalog]:
    norm_q = _normalize(query or "").strip()
    internal_types = _DOC_TYPE_INTERNAL.get(doc_type or "", []) if doc_type and doc_type != "any" else []

    def _type_filter(stmt):
        if not doc_type or doc_type == "any":
            return stmt
        conditions = []
        if internal_types:
            conditions.append(FileCatalog.doc_type.in_(internal_types))
        kws = _DOC_TYPE_KEYWORDS.get(doc_type, [])
        if kws:
            conditions.append(or_(*[FileCatalog.name_norm.like(f"%{k}%") for k in kws]))
        return stmt.where(or_(*conditions)) if conditions else stmt

    # Phase 0: doc_type-only query (no company name given) — search enriched doc_type column directly
    if not norm_q and doc_type and doc_type != "any" and internal_types:
        stmt = select(FileCatalog).where(FileCatalog.doc_type.in_(internal_types))
        stmt = stmt.order_by(nullslast(FileCatalog.modified.desc())).limit(limit)
        results = list(db.execute(stmt).scalars().all())
        if results:
            return results

    # Phase 1: company_norm match — precise, uses AI-enriched data
    if norm_q:
        stmt = select(FileCatalog).where(
            FileCatalog.company_norm.like(f"%{norm_q}%")
        )
        stmt = _type_filter(stmt)
        stmt = stmt.order_by(nullslast(FileCatalog.modified.desc())).limit(limit)
        results = list(db.execute(stmt).scalars().all())
        if results:
            return results

    # Phase 2: name_norm LIKE fallback (unenriched rows or broader keyword search)
    terms = [t for t in norm_q.split() if len(t) > 1]
    if not terms:
        return []
    stmt = select(FileCatalog)
    stmt = stmt.where(or_(*[FileCatalog.name_norm.like(f"%{t}%") for t in terms]))
    stmt = _type_filter(stmt)
    stmt = stmt.order_by(nullslast(FileCatalog.modified.desc())).limit(limit)
    return list(db.execute(stmt).scalars().all())


def _format_file_results(rows: list[FileCatalog], limit: int = MAX_FILE_RESULTS) -> str:
    if not rows:
        return "No matching files found in the catalog."

    from collections import defaultdict
    # Group by company (enriched) or folder path (fallback)
    groups: dict[str, list[FileCatalog]] = defaultdict(list)
    for r in rows:
        group_key = r.company or f"{r.drive}/{r.folder_path}".rstrip("/")
        groups[group_key].append(r)

    lines = [f"Found {len(rows)} file(s) across {len(groups)} group(s):"]
    for group_key, files in groups.items():
        # Show doc_type for this group if consistent
        types = {f.doc_type for f in files if f.doc_type}
        type_label = f" [{', '.join(sorted(types))}]" if types else ""
        lines.append(f"\n{group_key}{type_label} ({len(files)} files)")
        for r in files:
            date = r.modified.date().isoformat() if r.modified else "?"
            url = r.web_url or ""
            parts = [r.name, date]
            if r.doc_number:
                parts.append(f"Nr.{r.doc_number}")
            parts.append(url)
            lines.append(f"  - {' | '.join(parts)}")

    if len(rows) >= limit:
        lines.append("\n_(Showing newest 20 — there may be more. Narrow your search with a date or keyword.)_")
    return "\n".join(lines)

_openai: AsyncOpenAI | None = None


def _get_openai() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai


async def embed_text(text: str) -> list[float]:
    resp = await _get_openai().embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def _build_system_prompt(rules: list[AskRule]) -> str:
    active = sorted([r for r in rules if r.is_active], key=lambda r: r.priority)
    if not active:
        return _SYSTEM_BASE
    rules_block = "\n".join(f"{i+1}. {r.rule_text}" for i, r in enumerate(active))
    return f"{_SYSTEM_BASE}\n\nADMIN RULES:\n{rules_block}"


def search_chunks(query_embedding: list[float], db: Session, k: int = TOP_K) -> list[KnowledgeChunk]:
    results = db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.embedding.isnot(None))
        .where(KnowledgeChunk.source_type != "invenias")
        .order_by(KnowledgeChunk.embedding.cosine_distance(query_embedding))
        .limit(k)
    ).scalars().all()
    return results


async def ask_stream(
    question: str,
    db: Session,
    history: list[dict] | None = None,
) -> AsyncGenerator[str, None]:
    if not settings.openai_api_key:
        yield f"data: {json.dumps({'error': 'Ask PL requires OPENAI_API_KEY (used for embeddings).'})}\n\n"
        return

    try:
        query_vec = await embed_text(question)
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        msg = "OpenAI rate limit or quota exceeded." if "429" in str(e) or "rate" in str(e).lower() else "Could not process your question. Please try again."
        yield f"data: {json.dumps({'error': msg})}\n\n"
        return

    chunks = search_chunks(query_vec, db)

    # With no vector context AND no file catalog to search, there's nothing to answer from.
    catalog_has_files = db.execute(select(FileCatalog.id).limit(1)).first() is not None
    if not chunks and not (settings.anthropic_api_key and catalog_has_files):
        no_index_msg = "I don't have any indexed knowledge yet. Ask an admin to run indexing."
        yield f"data: {json.dumps({'text': no_index_msg})}\n\n"
        yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
        return

    context_parts = []
    seen_sources: dict[str, dict] = {}
    for chunk in chunks:
        date_str = chunk.modified.date().isoformat() if getattr(chunk, "modified", None) else ""
        header = f"[{chunk.source_name}{' | ' + date_str if date_str else ''}]"
        # Prepend AI summary as a quick orientation for Claude before the full content
        summary_prefix = f"Summary: {chunk.ai_summary}\n\n" if getattr(chunk, "ai_summary", None) else ""
        context_parts.append(f"{header}\n{summary_prefix}{chunk.content}")
        base_id = chunk.source_id.split("::")[0]
        if base_id not in seen_sources:
            seen_sources[base_id] = {"name": chunk.source_name, "url": chunk.source_url or ""}

    context_text = "\n\n---\n\n".join(context_parts) if context_parts else "(no documents matched the vector search; use the search_files tool to find files)"
    rules = db.execute(select(AskRule)).scalars().all()
    system_prompt = _build_system_prompt(rules)

    history_payload = [{"role": h["role"], "content": h["content"]} for h in (history or [])]
    messages_payload = [
        *history_payload,
        {"role": "user", "content": f"Context:\n{context_text}\n\nQuestion: {question}"},
    ]

    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            # Tool-use loop: Claude may call search_files (possibly several times)
            # before producing its final answer. Cap rounds to avoid runaway loops.
            for _round in range(4):
                async with client.messages.stream(
                    model=CHAT_MODEL_ANTHROPIC,
                    max_tokens=2048,
                    system=system_prompt,
                    tools=[SEARCH_FILES_TOOL],
                    messages=messages_payload,
                ) as stream:
                    async for text in stream.text_stream:
                        yield f"data: {json.dumps({'text': text})}\n\n"
                    final = await stream.get_final_message()

                if final.stop_reason != "tool_use":
                    break

                yield f"data: {json.dumps({'text': '\n\n*Ieškoma dokumentų...*\n\n'})}\n\n"
                messages_payload.append({"role": "assistant", "content": final.content})
                tool_results = []
                for block in final.content:
                    if block.type == "tool_use" and block.name == "search_files":
                        rows = search_files(
                            db,
                            (block.input or {}).get("query", ""),
                            (block.input or {}).get("doc_type"),
                        )
                        for r in rows:
                            if r.web_url and r.item_id not in seen_sources:
                                display = f"{r.folder_path}/{r.name}".lstrip("/") if r.folder_path else r.name
                                seen_sources[r.item_id] = {"name": display, "url": r.web_url}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _format_file_results(rows, MAX_FILE_RESULTS),
                        })
                if not tool_results:
                    break
                messages_payload.append({"role": "user", "content": tool_results})
        except Exception as e:
            logger.error(f"Anthropic chat failed: {type(e).__name__}: {e}")
            yield f"data: {json.dumps({'error': 'Could not generate a response. Please try again.'})}\n\n"
            return
    else:
        try:
            stream = await _get_openai().chat.completions.create(
                model=CHAT_MODEL_OPENAI,
                max_tokens=2048,
                stream=True,
                messages=[
                    {"role": "system", "content": system_prompt},
                    *messages_payload,
                ],
            )
            async for chunk in stream:
                text = chunk.choices[0].delta.content
                if text:
                    yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as e:
            logger.error(f"OpenAI chat failed: {type(e).__name__}: {e}")
            yield f"data: {json.dumps({'error': 'Could not generate a response. Please try again.'})}\n\n"
            return

    sources = list(seen_sources.values())
    yield f"data: {json.dumps({'done': True, 'sources': sources})}\n\n"
