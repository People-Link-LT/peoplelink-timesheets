import logging

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

    return StreamingResponse(
        ask_stream(question, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Admin — rules management
# ---------------------------------------------------------------------------

@router.get("/admin/rules", response_class=HTMLResponse)
def rules_page(request: Request, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    rules = db.execute(select(AskRule).order_by(AskRule.priority, AskRule.created_at)).scalars().all()
    return templates.TemplateResponse(request, "admin/rules.html", {"user": admin, "rules": rules})


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
    run_indexing_sync()
    return RedirectResponse("/ask/admin/rules", status_code=302)
