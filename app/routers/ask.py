import logging
import threading

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ask_engine import ask_stream
from app.auth import get_current_admin, get_current_user
from app.database import get_db
from app.models import AskRule, User
from app.templates import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ask")


@router.get("", response_class=HTMLResponse)
def ask_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "ask/index.html", {"user": user})


@router.post("/query")
async def ask_query(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        async def empty():
            import json
            yield f"data: {json.dumps({'error': 'Empty question.'})}\n\n"
        return StreamingResponse(empty(), media_type="text/event-stream")

    raw_history = body.get("history") or []
    history = [
        h for h in raw_history
        if isinstance(h, dict)
        and h.get("role") in ("user", "assistant")
        and isinstance(h.get("content"), str)
    ][-2:]  # keep last 1 turn (2 messages) — rate limit budget

    return StreamingResponse(
        ask_stream(question, db, history=history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Admin — rules management
# ---------------------------------------------------------------------------

@router.get("/admin/rules", response_class=HTMLResponse)
def rules_page(request: Request, db: Session = Depends(get_db), admin: User = Depends(get_current_admin), indexed: str = "", enriched: str = ""):
    from app.config import settings as _s
    from app.models import FileCatalog
    from sqlalchemy import func
    rules = db.execute(select(AskRule).order_by(AskRule.priority, AskRule.created_at)).scalars().all()
    total_files = db.execute(select(func.count(FileCatalog.id))).scalar() or 0
    enriched_files = db.execute(select(func.count(FileCatalog.id)).where(FileCatalog.enriched_at.isnot(None))).scalar() or 0
    return templates.TemplateResponse(request, "admin/rules.html", {
        "user": admin,
        "rules": rules,
        "indexing_started": bool(indexed),
        "enrichment_started": bool(enriched),
        "anthropic_key": bool(_s.anthropic_api_key),
        "total_files": total_files,
        "enriched_files": enriched_files,
    })


@router.post("/admin/rules/create")
def create_rule(
    name: str = Form(...),
    rule_text: str = Form(...),
    priority: int = Form(0),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    db.add(AskRule(name=name.strip(), rule_text=rule_text.strip(), priority=priority))
    db.commit()
    return RedirectResponse("/ask/admin/rules", status_code=302)


@router.post("/admin/rules/{rule_id}/toggle")
def toggle_rule(rule_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    rule = db.get(AskRule, rule_id)
    if rule:
        rule.is_active = not rule.is_active
        db.commit()
    return RedirectResponse("/ask/admin/rules", status_code=302)


@router.post("/admin/rules/{rule_id}/delete")
def delete_rule(rule_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    rule = db.get(AskRule, rule_id)
    if rule:
        db.delete(rule)
        db.commit()
    return RedirectResponse("/ask/admin/rules", status_code=302)


@router.post("/admin/index-now")
def trigger_index(admin: User = Depends(get_current_admin)):
    from app.indexer import run_indexing_sync
    threading.Thread(target=run_indexing_sync, daemon=True).start()
    return RedirectResponse("/ask/admin/rules?indexed=1", status_code=302)


@router.post("/admin/enrich-now")
def trigger_enrich(force: str = Form(""), admin: User = Depends(get_current_admin)):
    from app.enricher import run_enrichment_sync
    threading.Thread(target=run_enrichment_sync, kwargs={"force": force == "1"}, daemon=True).start()
    return RedirectResponse("/ask/admin/rules?enriched=1", status_code=302)


@router.get("/admin/catalog-debug")
async def catalog_debug(
    q: str = "",
    drive: str = "",
    doc_type: str = "",
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Diagnostic: show FileCatalog per-drive counts and run a test search."""
    from sqlalchemy import func, text
    from app.models import FileCatalog
    from app.ask_engine import search_files as _search_files

    # Per-drive counts
    rows = db.execute(
        select(FileCatalog.drive, func.count().label("total"),
               func.count(FileCatalog.enriched_at).label("enriched"),
               func.count(FileCatalog.company).label("with_company"))
        .group_by(FileCatalog.drive)
        .order_by(FileCatalog.drive)
    ).all()

    drive_stats = [
        {"drive": r.drive, "total": r.total, "enriched": r.enriched, "with_company": r.with_company}
        for r in rows
    ]

    # Sample rows for a specific drive
    sample = []
    if drive:
        sample_rows = db.execute(
            select(FileCatalog)
            .where(FileCatalog.drive == drive)
            .order_by(FileCatalog.modified.desc().nullslast())
            .limit(10)
        ).scalars().all()
        sample = [
            {
                "name": r.name, "folder_path": r.folder_path,
                "name_norm": r.name_norm, "company": r.company,
                "company_norm": r.company_norm, "doc_type": r.doc_type,
                "enriched_at": r.enriched_at.isoformat() if r.enriched_at else None,
            }
            for r in sample_rows
        ]

    # Test search_files
    search_results = []
    if q or doc_type:
        found = _search_files(db, q, doc_type=doc_type or None, limit=10)
        search_results = [
            {
                "name": r.name, "folder_path": r.folder_path, "drive": r.drive,
                "company": r.company, "company_norm": r.company_norm,
                "doc_type": r.doc_type, "doc_number": r.doc_number,
                "web_url": r.web_url,
            }
            for r in found
        ]

    return {
        "total_catalog_rows": sum(r["total"] for r in drive_stats),
        "drive_stats": drive_stats,
        "sample_from_drive": sample,
        "search_query": q,
        "search_doc_type": doc_type,
        "search_results": search_results,
    }


@router.get("/admin/stats")
async def stats(
    request: Request,
    q: str = "",
    key: str = "",
    db: Session = Depends(get_db),
):
    from fastapi import HTTPException
    from sqlalchemy import func
    from app.models import KnowledgeChunk
    from app.ask_engine import embed_text, search_chunks
    from app.config import settings as _s

    if key == _s.secret_key:
        pass  # key-based auth for programmatic access
    else:
        get_current_admin(request, db)  # falls back to JWT cookie

    counts = db.execute(
        select(KnowledgeChunk.source_type, func.count()).group_by(KnowledgeChunk.source_type)
    ).all()
    result = {"chunks": {row[0]: row[1] for row in counts}, "top_matches": []}

    if q:
        try:
            vec = await embed_text(q)
            chunks = search_chunks(vec, db, k=5)
            result["top_matches"] = [
                {"source": c.source_name, "url": c.source_url, "preview": c.content[:200]}
                for c in chunks
            ]
        except Exception as e:
            result["error"] = str(e)

    return result
