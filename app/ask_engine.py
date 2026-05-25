import json
import logging
from typing import AsyncGenerator

import anthropic
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, AskRule

logger = logging.getLogger(__name__)

_SYSTEM_BASE = """You are Ask PL, the internal AI assistant for People Link employees.
Answer questions using only the provided context below. If the answer is not in the context, clearly say you don't have that information in your knowledge base — never make things up.
Always cite the source name for each fact you state (e.g. "According to [Assignment PL-001 — Acme Corp]...").
Be concise and professional."""

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 1536
CHAT_MODEL = "claude-haiku-4-5-20251001"
TOP_K = 8


def _openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.openai_api_key)


def _anthropic_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def embed_text(text: str) -> list[float]:
    client = _openai_client()
    resp = await client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def _build_system_prompt(rules: list[AskRule]) -> str:
    active = sorted([r for r in rules if r.is_active], key=lambda r: r.priority)
    if not active:
        return _SYSTEM_BASE
    rules_block = "\n".join(f"{i+1}. {r.rule_text}" for i, r in enumerate(active))
    return f"{_SYSTEM_BASE}\n\nADMIN RULES:\n{rules_block}"


def search_chunks(query_embedding: list[float], db: Session, k: int = TOP_K) -> list[KnowledgeChunk]:
    from pgvector.sqlalchemy import Vector
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
    if not settings.openai_api_key or not settings.anthropic_api_key:
        yield f"data: {json.dumps({'error': 'Ask PL is not configured — API keys missing.'})}\n\n"
        return

    try:
        query_vec = await embed_text(question)
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        msg = "OpenAI rate limit or quota exceeded — check platform.openai.com billing." if "429" in str(e) or "rate" in str(e).lower() else "Could not process your question. Please try again."
        yield f"data: {json.dumps({'error': msg})}\n\n"
        return

    chunks = search_chunks(query_vec, db)
    if not chunks:
        no_index_msg = "I don't have any indexed knowledge yet. Ask an admin to run indexing."
        yield f"data: {json.dumps({'text': no_index_msg})}\n\n"
        yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
        return

    context_parts = []
    seen_sources: dict[str, str] = {}
    for chunk in chunks:
        context_parts.append(f"[{chunk.source_name}]\n{chunk.content}")
        if chunk.source_id not in seen_sources:
            seen_sources[chunk.source_id] = chunk.source_name

    context_text = "\n\n---\n\n".join(context_parts)

    rules = db.execute(select(AskRule)).scalars().all()
    system_prompt = _build_system_prompt(rules)

    client = _anthropic_client()
    try:
        async with client.messages.stream(
            model=CHAT_MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": f"Context:\n{context_text}\n\nQuestion: {question}",
                }
            ],
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
    except Exception as e:
        logger.error(f"Anthropic streaming failed: {type(e).__name__}: {e}")
        yield f"data: {json.dumps({'error': f'Response generation failed: {type(e).__name__}'})}\n\n"
        return

    sources = [{"name": name} for name in seen_sources.values()]
    yield f"data: {json.dumps({'done': True, 'sources': sources})}\n\n"
