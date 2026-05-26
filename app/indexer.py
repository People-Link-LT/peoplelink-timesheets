import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Assignment, KnowledgeChunk
from app.ask_engine import embed_text
from app.config import settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 800      # words per chunk
CHUNK_OVERLAP = 100   # word overlap between chunks
INVENIAS_SUBDOMAIN = "peoplelink"


def _chunk_text(text: str) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + CHUNK_SIZE])
        if chunk.strip():
            chunks.append(chunk)
    return chunks


def _assignment_url(item_id: str) -> str:
    return f"https://{INVENIAS_SUBDOMAIN}.invenias.com/a/assignments/{item_id}"


def _assignment_text(a: Assignment) -> str:
    return (
        f"Assignment Reference: {a.reference_number}\n"
        f"Company: {a.company_name or 'Unknown'}\n"
        f"Role / Title: {a.title or 'Unknown'}\n"
        f"Status: {a.status}\n"
        f"Invenias URL: {_assignment_url(a.id)}"
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

        url = _assignment_url(assignment.id)
        if existing:
            existing.content = content
            existing.source_name = assignment.display_name
            existing.source_url = url
            existing.embedding = embedding
            existing.indexed_at = now
        else:
            db.add(KnowledgeChunk(
                source_type="invenias",
                source_id=assignment.id,
                source_name=assignment.display_name,
                source_url=url,
                content=content,
                embedding=embedding,
                indexed_at=now,
            ))
        indexed += 1

    db.commit()
    return indexed


async def _index_one_drive(db: Session, drive_name: str, sp_kwargs: dict, http, now: datetime) -> int:
    from app.sharepoint import list_files, resolve_token_and_drive

    SUPPORTED_EXTENSIONS = {".txt", ".md", ".csv", ".pdf", ".docx", ".pptx", ".xlsx"}
    indexed = 0
    skipped_ext = 0

    token, drive_base = await resolve_token_and_drive(**sp_kwargs, drive_name=drive_name)
    auth_headers = {"Authorization": f"Bearer {token}"}

    async def collect_files(folder: str) -> list[dict]:
        try:
            items = await list_files(**sp_kwargs, drive_name=drive_name, folder=folder)
        except Exception as e:
            logger.error(f"[{drive_name}] List failed for '{folder}': {e}")
            return []
        result = []
        for item in items:
            if item["is_folder"]:
                sub_folder = f"{folder}/{item['name']}".lstrip("/") if folder else item["name"]
                result.extend(await collect_files(sub_folder))
            else:
                result.append(item)
        return result

    all_files = await collect_files("")
    logger.info(f"[{drive_name}] Found {len(all_files)} files")

    import io as _io

    for f in all_files:
        name: str = f["name"]
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in SUPPORTED_EXTENSIONS:
            skipped_ext += 1
            continue

        try:
            resp = await http.get(
                f"{drive_base}/items/{f['id']}/content",
                headers=auth_headers,
                follow_redirects=True,
            )
            resp.raise_for_status()
            raw = resp.content
        except Exception as e:
            logger.error(f"[{drive_name}] Download failed for {name}: {e}")
            continue

        try:
            if ext == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(_io.BytesIO(raw))
                full_text = "\n".join(p.extract_text() or "" for p in reader.pages)
            elif ext == ".docx":
                from docx import Document
                doc = Document(_io.BytesIO(raw))
                full_text = "\n".join(p.text for p in doc.paragraphs)
            elif ext == ".pptx":
                from pptx import Presentation
                prs = Presentation(_io.BytesIO(raw))
                slide_texts = []
                for slide in prs.slides:
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text.strip():
                            slide_texts.append(shape.text)
                full_text = "\n".join(slide_texts)
            elif ext == ".xlsx":
                import openpyxl
                wb = openpyxl.load_workbook(_io.BytesIO(raw), read_only=True, data_only=True)
                rows = []
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        row_text = " ".join(str(c) for c in row if c is not None)
                        if row_text.strip():
                            rows.append(row_text)
                full_text = "\n".join(rows)
            else:
                full_text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            logger.error(f"[{drive_name}] Text extraction failed for {name}: {e}")
            continue

        chunks = _chunk_text(full_text)
        if not chunks:
            continue

        # Delete existing chunks for this file before re-inserting
        db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.source_type == "sharepoint",
                KnowledgeChunk.source_id.like(f"{f['id']}%"),
            )
        )

        for i, chunk_text_content in enumerate(chunks):
            try:
                embedding = await embed_text(chunk_text_content)
            except Exception as e:
                logger.error(f"[{drive_name}] Embedding failed for {name} chunk {i}: {e}")
                continue

            chunk_id = f"{f['id']}::{i}" if i > 0 else f["id"]
            db.add(KnowledgeChunk(
                source_type="sharepoint",
                source_id=chunk_id,
                source_name=name,
                source_url=f.get("web_url"),
                content=chunk_text_content,
                embedding=embedding,
                indexed_at=now,
            ))
            indexed += 1

    db.commit()
    logger.info(f"[{drive_name}] Skipped {skipped_ext} files with unsupported extensions")
    return indexed


async def _index_sharepoint(db: Session) -> int:
    if not all([
        settings.sharepoint_tenant_id,
        settings.sharepoint_client_id,
        settings.sharepoint_client_secret,
    ]):
        return 0

    import httpx

    drives_raw = settings.sharepoint_index_drives or settings.sharepoint_drive_name
    drives = [d.strip() for d in drives_raw.split(",") if d.strip()]
    if not drives:
        logger.warning("No drives configured for indexing (SHAREPOINT_INDEX_DRIVES not set)")
        return 0

    sp_kwargs = dict(
        tenant_id=settings.sharepoint_tenant_id,
        client_id=settings.sharepoint_client_id,
        client_secret=settings.sharepoint_client_secret,
        site_hostname=settings.sharepoint_site_hostname,
        site_path=settings.sharepoint_site_path,
    )

    now = datetime.now(timezone.utc)
    total = 0

    async with httpx.AsyncClient(timeout=60) as http:
        for drive_name in drives:
            logger.info(f"Indexing drive: {drive_name}")
            try:
                count = await _index_one_drive(db, drive_name, sp_kwargs, http, now)
                logger.info(f"[{drive_name}] Indexed {count} chunks")
                total += count
            except Exception as e:
                logger.error(f"[{drive_name}] Drive indexing failed: {e}")

    return total


async def _run_indexing() -> dict:
    if not settings.openai_api_key:
        return {"ok": False, "message": "OPENAI_API_KEY not set — skipping indexing."}

    db = SessionLocal()
    try:
        invenias_count = await _index_assignments(db)
        sp_count = await _index_sharepoint(db)
        msg = f"Indexed {invenias_count} Invenias assignments + {sp_count} SharePoint chunks"
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
