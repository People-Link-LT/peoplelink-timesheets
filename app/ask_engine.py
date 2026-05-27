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
Answer questions using the provided context and the search_files tool. If the answer is not available, clearly say you don't have that information in your knowledge base — never make things up.
Always respond in the same language the user asked in (Lithuanian if they asked in Lithuanian, English if in English).

You have a search_files tool that searches the full SharePoint document catalog by company name or keyword. Use it whenever the user wants to find or list specific documents — invoices (sąskaitos), proposals (pasiūlymai), or contracts (sutartys) — for a company. The vector context below only holds a small sample of documents, so for "find/list all X for company Y" questions you MUST call search_files rather than relying on the context. When listing files, show each file name as a clickable markdown link to its URL, grouped sensibly, newest first.
When using search_files, pass only the company name or core keyword as the query — do not include document-type words (like "sąskaita", "invoice", "pasiūlymas") in the query; use the doc_type parameter for that instead. If results are empty, retry with a shorter or simpler form of the company name.

For general knowledge questions, answer from the context below.
Always cite the source document for each fact you state. If a URL is available, make it a clickable link: e.g. "According to [Augimo sistema 2025.docx](https://...)..."
When the same topic appears in multiple document versions, always prefer and cite the document with the most recent date (shown in brackets next to the file name in the context).
Be concise and professional. For list-style questions, use bullet points."""

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL_OPENAI = "gpt-4o-mini"
CHAT_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"  # upgrade to claude-sonnet-4-6 when org rate limit is raised
TOP_K = 5           # keep both API rounds under 10K tokens/min (org rate limit)
MAX_FILE_RESULTS = 20

# Keyword filters per document type (matched against the diacritic-stripped catalog text)
_DOC_TYPE_KEYWORDS = {
    "invoice":  ["saskait", "faktura", "invoice"],
    "proposal": ["pasiulym", "proposal"],
    "contract": ["sutart", "contract"],
}

SEARCH_FILES_TOOL = {
    "name": "search_files",
    "description": (
        "Search the People Link SharePoint document catalog by company name or keyword found "
        "in the file name or folder path. Use this to find or list specific documents such as "
        "invoices (sąskaitos), proposals (pasiūlymai), or contracts (sutartys) for a company. "
        "Returns up to 60 matching files with their SharePoint links, newest first. "
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
    terms = [t for t in _normalize(query or "").split() if len(t) > 1]
    stmt = select(FileCatalog)
    if terms:
        stmt = stmt.where(or_(*[FileCatalog.name_norm.like(f"%{t}%") for t in terms]))
    if doc_type and doc_type != "any":
        kws = _DOC_TYPE_KEYWORDS.get(doc_type, [])
        if kws:
            stmt = stmt.where(or_(*[FileCatalog.name_norm.like(f"%{k}%") for k in kws]))
    stmt = stmt.order_by(nullslast(FileCatalog.modified.desc())).limit(limit)
    return list(db.execute(stmt).scalars().all())


def _format_file_results(rows: list[FileCatalog], limit: int = MAX_FILE_RESULTS) -> str:
    if not rows:
        return "No matching files found in the catalog."
    lines = [f"Found {len(rows)} matching file(s):"]
    for r in rows:
        loc = f"{r.drive}/{r.folder_path}".rstrip("/")
        date = r.modified.date().isoformat() if r.modified else "unknown date"
        url = r.web_url or ""
        lines.append(f"- {r.name} | folder: {loc} | modified: {date} | url: {url}")
    if len(rows) >= limit:
        lines.append("_(Showing newest 60 — there may be more. Try narrowing your search with a date or keyword.)_")
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
        context_parts.append(f"{header}\n{chunk.content}")
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
                                seen_sources[r.item_id] = {"name": r.name, "url": r.web_url}
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
