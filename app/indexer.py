import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Assignment, KnowledgeChunk
from app.ask_engine import embed_text
from app.config import settings

logger = logging.getLogger(__name__)


def _assignment_text(a: Assignment) -> str:
    return (
        f"Assignment Reference: {a.reference_number}\n"
        f"Company: {a.company_name or 'Unknown'}\n"
        f"Role / Title: {a.title or 'Unknown'}\n"
        f"Status: {a.status}"
    )


async def _index_assignments(db: Session) -> int:
    assignments = db.execute(select(Assignment)).scalars().all()
    now = datetime.now(timezone.utc)
    indexed = 0

    for assignment in assignments:
        content = _assignment_text(assignment)
        try:
            embedding = await embed_text(content)
        except Exception as e:
            logger.error(f"Embedding failed for assignment {assignment.id}: {e}")
            continue

        existing = db.execute(
            select(KnowledgeChunk).where(
                KnowledgeChunk.source_type == "invenias",
                KnowledgeChunk.source_id == assignment.id,
            )
        ).scalar_one_or_none()

        if existing:
            existing.content = content
            existing.source_name = assignment.display_name
            existing.embedding = embedding
            existing.indexed_at = now
        else:
            db.add(KnowledgeChunk(
                source_type="invenias",
                source_id=assignment.id,
                source_name=assignment.display_name,
                content=content,
                embedding=embedding,
                indexed_at=now,
            ))
        indexed += 1

    db.commit()
    return indexed


async def _index_sharepoint(db: Session) -> int:
    if not all([
        settings.sharepoint_tenant_id,
        settings.sharepoint_client_id,
        settings.sharepoint_client_secret,
    ]):
        return 0

    from app.sharepoint import list_files
    import httpx
    import io as _io

    sp_kwargs = dict(
        tenant_id=settings.sharepoint_tenant_id,
        client_id=settings.sharepoint_client_id,
        client_secret=settings.sharepoint_client_secret,
        site_hostname=settings.sharepoint_site_hostname,
        site_path=settings.sharepoint_site_path,
        drive_name=settings.sharepoint_drive_name,
    )

    SUPPORTED_EXTENSIONS = {".txt", ".md", ".csv", ".pdf", ".docx"}
    now = datetime.now(timezone.utc)
    indexed = 0

    # Collect all files recursively across subfolders
    async def collect_files(folder: str) -> list[dict]:
        try:
            items = await list_files(**sp_kwargs, folder=folder)
        except Exception as e:
            logger.error(f"SharePoint list failed for '{folder}': {e}")
            return []
        result = []
        for item in items:
            if item["is_folder"]:
                sub = await collect_files(f"{folder}/{item['name']}".lstrip("/") if folder else item["name"])
                result.extend(sub)
            else:
                result.append(item)
        return result

    root_folder = settings.sharepoint_documents_folder or ""
    all_files = await collect_files(root_folder)
    logger.info(f"SharePoint indexing: found {len(all_files)} files under '{root_folder}'")

    async with httpx.AsyncClient(timeout=60) as http:
        for f in all_files:
            name: str = f["name"]
            ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            download_url = f.get("download_url")
            if not download_url:
                continue

            try:
                resp = await http.get(download_url)
                resp.raise_for_status()
                raw = resp.content
            except Exception as e:
                logger.error(f"Failed to download {name}: {e}")
                continue

            try:
                if ext == ".pdf":
                    from pypdf import PdfReader
                    reader = PdfReader(_io.BytesIO(raw))
                    content_text = "\n".join(p.extract_text() or "" for p in reader.pages)
                elif ext == ".docx":
                    from docx import Document
                    doc = Document(_io.BytesIO(raw))
                    content_text = "\n".join(p.text for p in doc.paragraphs)
                else:
                    content_text = raw.decode("utf-8", errors="replace")
                content_text = content_text[:8000]
            except Exception as e:
                logger.error(f"Failed to extract text from {name}: {e}")
                continue

            try:
                embedding = await embed_text(content_text)
            except Exception as e:
                logger.error(f"Embedding failed for {name}: {e}")
                continue

            existing = db.execute(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.source_type == "sharepoint",
                    KnowledgeChunk.source_id == f["id"],
                )
            ).scalar_one_or_none()

            if existing:
                existing.content = content_text
                existing.source_name = name
                existing.source_url = f.get("web_url")
                existing.embedding = embedding
                existing.indexed_at = now
            else:
                db.add(KnowledgeChunk(
                    source_type="sharepoint",
                    source_id=f["id"],
                    source_name=name,
                    source_url=f.get("web_url"),
                    content=content_text,
                    embedding=embedding,
                    indexed_at=now,
                ))
            indexed += 1

    db.commit()
    return indexed


async def _run_indexing() -> dict:
    if not settings.openai_api_key:
        return {"ok": False, "message": "OPENAI_API_KEY not set — skipping indexing."}

    db = SessionLocal()
    try:
        invenias_count = await _index_assignments(db)
        sp_count = await _index_sharepoint(db)
        msg = f"Indexed {invenias_count} Invenias assignments"
        if sp_count:
            msg += f" + {sp_count} SharePoint documents"
        logger.info(msg)
        return {"ok": True, "message": msg}
    except Exception as e:
        db.rollback()
        logger.error(f"Indexing failed: {e}")
        return {"ok": False, "message": str(e)}
    finally:
        db.close()


def run_indexing_sync() -> dict:
    return asyncio.run(_run_indexing())
