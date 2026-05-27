import asyncio
import json
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, AskRule

logger = logging.getLogger(__name__)

_SYSTEM_BASE = """You are Ask PL, the internal AI assistant for People Link employees.
Answer questions using only the provided context below. If the answer is not in the context, clearly say you don't have that information in your knowledge base — never make things up.
Always respond in the same language the user asked in (Lithuanian if they asked in Lithuanian, English if in English).
Always cite the source document for each fact you state. If a URL is available, make it a clickable link: e.g. "According to [Augimo sistema 2025.docx](https://...)..."
When multiple documents cover the same topic, prefer the most recent one.
Be concise and professional. For list-style questions, use bullet points."""

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL_OPENAI = "gpt-4o-mini"
CHAT_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
TOP_K = 15

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
    if not chunks:
        no_index_msg = "I don't have any indexed knowledge yet. Ask an admin to run indexing."
        yield f"data: {json.dumps({'text': no_index_msg})}\n\n"
        yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
        return

    context_parts = []
    seen_sources: dict[str, dict] = {}
    for chunk in chunks:
        context_parts.append(f"[{chunk.source_name}]\n{chunk.content}")
        base_id = chunk.source_id.split("::")[0]
        if base_id not in seen_sources:
            seen_sources[base_id] = {"name": chunk.source_name, "url": chunk.source_url or ""}

    context_text = "\n\n---\n\n".join(context_parts)
    rules = db.execute(select(AskRule)).scalars().all()
    system_prompt = _build_system_prompt(rules)

    messages_payload = [{"role": "user", "content": f"Context:\n{context_text}\n\nQuestion: {question}"}]

    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            async with client.messages.stream(
                model=CHAT_MODEL_ANTHROPIC,
                max_tokens=2048,
                system=system_prompt,
                messages=messages_payload,
            ) as stream:
                async for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
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
