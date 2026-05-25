import asyncio
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
Always respond in the same language the user asked in (Lithuanian if they asked in Lithuanian, English if in English).
Always cite the source document for each fact you state. If a URL is available, make it a clickable link: e.g. "According to [Augimo sistema 2025.docx](https://...)..."
When multiple documents cover the same topic, prefer the most recent one.
Be concise and professional. For list-style questions, use bullet points."""

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 1536
CHAT_MODEL = "claude-haiku-4-5-20251001"
TOP_K = 15

# Singleton clients — avoids creating new connection pools per request
_openai: AsyncOpenAI | None = None
_anthropic: anthropic.AsyncAnthropic | None = None


def _get_openai() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai


def _get_anthropic() -> anthropic.AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        import httpx
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            transport=httpx.AsyncHTTPTransport(retries=2),
        )
        _anthropic = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            http_client=http_client,
        )
    return _anthropic


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
    from pgvector.sqlalchemy import Vector
    results = db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.embedding.isnot(None))
        .order_by(KnowledgeChunk.embedding.cosine_distance(query_embedding))
        .limit(k)
    ).scalars().all()
    return results


async def _call_anthropic(system_prompt: str, user_content: str) -> str:
    """Non-streaming call with retry. Returns full response text."""
    client = _get_anthropic()
    last_err = None
    for attempt in range(3):
        try:
            msg = await client.messages.create(
                model=CHAT_MODEL,
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            return msg.content[0].text
        except Exception as e:
            last_err = e
            logger.warning(f"Anthropic attempt {attempt + 1} failed: {type(e).__name__}: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    raise last_err


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
    seen_sources: dict[str, dict] = {}
    for chunk in chunks:
        context_parts.append(f"[{chunk.source_name}]\n{chunk.content}")
        base_id = chunk.source_id.split("::")[0]
        if base_id not in seen_sources:
            seen_sources[base_id] = {"name": chunk.source_name, "url": chunk.source_url or ""}

    context_text = "\n\n---\n\n".join(context_parts)
    rules = db.execute(select(AskRule)).scalars().all()
    system_prompt = _build_system_prompt(rules)
    user_content = f"Context:\n{context_text}\n\nQuestion: {question}"

    # Try streaming first; fall back to non-streaming on connection error
    try:
        client = _get_anthropic()
        async with client.messages.stream(
            model=CHAT_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
    except anthropic.APIConnectionError as e:
        logger.warning(f"Streaming connection error, falling back to non-streaming: {e}")
        try:
            full_text = await _call_anthropic(system_prompt, user_content)
            # Yield in small chunks to simulate streaming feel
            words = full_text.split(" ")
            for i in range(0, len(words), 8):
                chunk_text = " ".join(words[i:i+8])
                if i + 8 < len(words):
                    chunk_text += " "
                yield f"data: {json.dumps({'text': chunk_text})}\n\n"
                await asyncio.sleep(0.02)
        except Exception as e2:
            logger.error(f"Anthropic fallback also failed: {type(e2).__name__}: {e2}")
            yield f"data: {json.dumps({'error': 'Could not reach the AI service. Please try again in a moment.'})}\n\n"
            return
    except Exception as e:
        logger.error(f"Anthropic streaming failed: {type(e).__name__}: {e}")
        yield f"data: {json.dumps({'error': 'Could not reach the AI service. Please try again in a moment.'})}\n\n"
        return

    sources = list(seen_sources.values())
    yield f"data: {json.dumps({'done': True, 'sources': sources})}\n\n"
