"""
Bulk AI description generation for the Document Library.

Iterates over all FileCatalog rows and all _CATEGORIES (top-level + subcategories)
and populates DocMeta.comment where it is missing (or everywhere if force=True).

Three phases:
  A. Files with KnowledgeChunk.ai_summary  → copy directly, no API call
  B. Files with content chunks but no summary → call _ai_generate_description
  C. Categories / subcategories             → call _ai_generate_description
         with a sample of filenames from that drive
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import DocMeta, FileCatalog, KnowledgeChunk

logger = logging.getLogger(__name__)

_CONCURRENCY = 3
_CALL_SPACING = 1.0
_BATCH_COMMIT = 50


def _build_template(row: FileCatalog) -> str:
    """Build a short description from FileCatalog enrichment metadata — no API call."""
    if not row.doc_type or row.doc_type == "other":
        return ""
    parts = [row.doc_type.replace("_", " ").capitalize()]
    if row.company:
        parts.append(f"with {row.company}")
    if row.doc_number:
        parts.append(f"Nr. {row.doc_number}")
    if row.doc_year:
        yr = str(row.doc_year)
        if row.doc_month:
            yr += f"-{row.doc_month:02d}"
        parts.append(f"({yr})")
    return " ".join(parts)


def _upsert_meta(db: Session, item_id: str, drive: str, path: str, name: str,
                 comment: str, model: str) -> None:
    existing = db.execute(
        select(DocMeta).where(DocMeta.item_id == item_id)
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if existing:
        existing.comment = comment
        existing.ai_generated = True
        existing.ai_model = model
        existing.updated_at = now
    else:
        db.add(DocMeta(
            item_id=item_id,
            drive=drive,
            path=path,
            name=name,
            comment=comment,
            ai_generated=True,
            ai_model=model,
            updated_at=now,
        ))


async def describe_all(db: Session, force: bool = False) -> dict:
    from app import progress as _prog
    from app.routers.documents import _CATEGORIES, _ai_generate_description

    # ── pre-load existing DocMeta ──────────────────────────────────────────
    existing_meta: dict[str, DocMeta] = {
        r.item_id: r for r in db.execute(select(DocMeta)).scalars().all()
    }

    # ── pre-load KnowledgeChunks ──────────────────────────────────────────
    all_chunks: list[KnowledgeChunk] = list(
        db.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.source_type == "sharepoint")
        ).scalars().all()
    )
    summary_by_item: dict[str, str] = {}
    chunks_by_item: dict[str, list[KnowledgeChunk]] = defaultdict(list)
    for c in all_chunks:
        base = c.source_id.split("::")[0]
        chunks_by_item[base].append(c)
        if c.ai_summary and base not in summary_by_item:
            summary_by_item[base] = c.ai_summary

    # ── FileCatalog rows ──────────────────────────────────────────────────
    cat_rows: list[FileCatalog] = list(
        db.execute(select(FileCatalog)).scalars().all()
    )

    # Determine which files need processing
    files_phase_a: list[FileCatalog] = []   # have ai_summary → free copy
    files_phase_b_tmpl: list[FileCatalog] = []  # have doc_type → template, no API
    files_phase_b_ai: list[FileCatalog] = []    # have chunks, need API call

    for row in cat_rows:
        meta = existing_meta.get(row.item_id)
        if meta and meta.comment and not force:
            continue
        if row.item_id in summary_by_item:
            files_phase_a.append(row)
        elif row.doc_type and row.doc_type != "other":
            files_phase_b_tmpl.append(row)
        elif row.item_id in chunks_by_item:
            files_phase_b_ai.append(row)
        # else: no content at all — skip

    # ── categories that need description ─────────────────────────────────
    cats_to_describe: list[dict] = []
    for cat in _CATEGORIES:
        key = cat["key"]
        meta = existing_meta.get(key)
        if meta and meta.comment and not force:
            continue
        if cat.get("drive"):
            cats_to_describe.append({"key": key, "name": cat["name"], "drive": cat["drive"]})
        for sub in cat.get("subcategories", []):
            sub_key = f"subcat::{cat['name']}::{sub['name']}"
            sub_meta = existing_meta.get(sub_key)
            if sub_meta and sub_meta.comment and not force:
                continue
            if sub.get("drive"):
                cats_to_describe.append({"key": sub_key, "name": sub["name"], "drive": sub["drive"]})

    total = len(files_phase_a) + len(files_phase_b_tmpl) + len(files_phase_b_ai) + len(cats_to_describe)
    _prog.update("auto_describe", running=True, done=0, total=total)
    logger.info(f"Auto-describe: {total} items ({len(files_phase_a)} from summary, "
                f"{len(files_phase_b_tmpl)} template, {len(files_phase_b_ai)} AI files, "
                f"{len(cats_to_describe)} categories)")

    done = 0

    # ── Phase A: copy ai_summary ──────────────────────────────────────────
    for row in files_phase_a:
        desc = summary_by_item[row.item_id]
        _upsert_meta(db, row.item_id, row.drive, row.folder_path, row.name, desc, "ai_summary")
        done += 1
        if done % _BATCH_COMMIT == 0:
            db.commit()
            _prog.update("auto_describe", done=done)

    db.commit()
    _prog.update("auto_describe", done=done)

    # ── Phase B-template: build from FileCatalog metadata ─────────────────
    for row in files_phase_b_tmpl:
        desc = _build_template(row)
        if desc:
            _upsert_meta(db, row.item_id, row.drive, row.folder_path, row.name, desc, "template")
        done += 1
        if done % _BATCH_COMMIT == 0:
            db.commit()
            _prog.update("auto_describe", done=done)

    db.commit()
    _prog.update("auto_describe", done=done)

    # ── Phase B-AI: generate from chunk content ───────────────────────────
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _describe_file(row: FileCatalog) -> None:
        nonlocal done
        chunks = sorted(
            chunks_by_item[row.item_id],
            key=lambda c: int(c.source_id.split("::")[-1]) if "::" in c.source_id else 0,
        )
        sample = "\n\n".join(c.content[:500] for c in chunks[:4])
        async with sem:
            desc, model = await _ai_generate_description(row.name, row.drive, sample)
            await asyncio.sleep(_CALL_SPACING)
        if desc:
            _upsert_meta(db, row.item_id, row.drive, row.folder_path, row.name, desc, model)
        done += 1
        _prog.update("auto_describe", done=done)

    # Process in batches of _BATCH_COMMIT to commit periodically
    for i in range(0, len(files_phase_b_ai), _BATCH_COMMIT):
        batch = files_phase_b_ai[i: i + _BATCH_COMMIT]
        await asyncio.gather(*[_describe_file(r) for r in batch])
        db.commit()

    # ── Phase C: categories ───────────────────────────────────────────────
    async def _describe_cat(entry: dict) -> None:
        nonlocal done
        drive = entry["drive"]
        sample_rows = db.execute(
            select(FileCatalog.name)
            .where(FileCatalog.drive == drive)
            .limit(15)
        ).scalars().all()
        if sample_rows:
            sample = f"Files in this folder:\n" + "\n".join(f"• {n}" for n in sample_rows)
        else:
            sample = f"Document library: {drive}"
        async with sem:
            desc, model = await _ai_generate_description(entry["name"], drive, sample)
            await asyncio.sleep(_CALL_SPACING)
        if desc:
            _upsert_meta(db, entry["key"], drive, "", entry["name"], desc, model)
        done += 1
        _prog.update("auto_describe", done=done)

    for entry in cats_to_describe:
        await _describe_cat(entry)
        db.commit()

    logger.info(f"Auto-describe complete: {done} items processed")
    return {
        "files": len(files_phase_a) + len(files_phase_b_tmpl) + len(files_phase_b_ai),
        "categories": len(cats_to_describe),
    }


def run_auto_describe_sync(force: bool = False) -> dict:
    from app import progress as _prog
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        result = asyncio.run(describe_all(db, force))
        msg = f"Done: {result['files']} files, {result['categories']} categories described"
        _prog.update("auto_describe", running=False, last_ok=True, last_message=msg,
                     last_finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
        logger.info(msg)
        return result
    except Exception as e:
        logger.error(f"Auto-describe failed: {e}")
        _prog.update("auto_describe", running=False, last_ok=False, last_message=str(e))
        return {"files": 0, "categories": 0}
    finally:
        db.close()
