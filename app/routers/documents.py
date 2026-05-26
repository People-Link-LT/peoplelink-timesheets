import csv
import io
import json as _json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Assignment, DocMeta, TimesheetEntry, User, Week
from app.templates import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents")


def _sp_configured() -> bool:
    return all([
        settings.sharepoint_tenant_id,
        settings.sharepoint_client_id,
        settings.sharepoint_client_secret,
        settings.sharepoint_site_hostname,
    ])


def _sp_base_kwargs() -> dict:
    return dict(
        tenant_id=settings.sharepoint_tenant_id,
        client_id=settings.sharepoint_client_id,
        client_secret=settings.sharepoint_client_secret,
        site_hostname=settings.sharepoint_site_hostname,
        site_path=settings.sharepoint_site_path,
    )


def _sp_kwargs(drive_name: str = "") -> dict:
    return {**_sp_base_kwargs(), "drive_name": drive_name or settings.sharepoint_drive_name}


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def browse(
    request: Request,
    folder: str = "",
    drive: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    files, error = [], None

    if _sp_configured():
        try:
            if not drive:
                from app.sharepoint import list_drives
                files = await list_drives(**_sp_base_kwargs())
            else:
                from app.sharepoint import list_files
                files = await list_files(**_sp_kwargs(drive), folder=folder)
        except Exception as e:
            logger.error(f"Browse error: {e}")
            error = str(e)
    else:
        error = "SharePoint is not configured. Set SHAREPOINT_* environment variables."

    breadcrumb = [p for p in folder.split("/") if p] if folder else []

    item_ids = [f["id"] for f in files if f.get("id")]
    metas: dict = {}
    if item_ids:
        rows = db.execute(select(DocMeta).where(DocMeta.item_id.in_(item_ids))).scalars().all()
        metas = {
            r.item_id: {
                "comment": r.comment or "",
                "audience": _json.loads(r.audience) if r.audience else [],
            }
            for r in rows
        }

    return templates.TemplateResponse(request, "documents/browse.html", {
        "user": user,
        "files": files,
        "error": error,
        "current_folder": folder,
        "current_drive": drive,
        "breadcrumb": breadcrumb,
        "metas": metas,
    })


# ---------------------------------------------------------------------------
# Metadata API — save comment + audience for a SharePoint item
# ---------------------------------------------------------------------------

@router.post("/api/meta", response_class=JSONResponse)
async def save_meta(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    body = await request.json()
    item_id = (body.get("item_id") or "").strip()
    if not item_id:
        return JSONResponse({"ok": False, "error": "item_id required"}, status_code=400)

    comment = (body.get("comment") or "").strip()
    audience = body.get("audience") or []
    if isinstance(audience, str):
        audience = [a.strip() for a in audience.split(",") if a.strip()]

    existing = db.execute(select(DocMeta).where(DocMeta.item_id == item_id)).scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if existing:
        existing.comment = comment or None
        existing.audience = _json.dumps(audience) if audience else None
        existing.updated_by = user.full_name
        existing.updated_at = now
    else:
        db.add(DocMeta(
            item_id=item_id,
            drive=body.get("drive", ""),
            path=body.get("path", ""),
            name=body.get("name", ""),
            comment=comment or None,
            audience=_json.dumps(audience) if audience else None,
            updated_by=user.full_name,
            updated_at=now,
        ))
    db.commit()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# File proxy — stream a SharePoint file through the app for inline viewing
# ---------------------------------------------------------------------------

@router.get("/file/{item_id}")
async def file_proxy(
    item_id: str,
    drive: str = "",
    user: User = Depends(get_current_user),
):
    if not _sp_configured():
        return Response("SharePoint not configured", status_code=503)

    try:
        from app.sharepoint import get_file_stream
        content, mime_type, filename = await get_file_stream(**_sp_kwargs(drive), item_id=item_id)
        return Response(
            content=content,
            media_type=mime_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"File proxy error for {item_id}: {e}")
        return Response(f"Could not fetch file: {e}", status_code=502)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "documents/upload.html", {
        "user": user,
        "default_folder": settings.sharepoint_documents_folder or "Documents",
        "success": None,
        "error": None,
    })


@router.post("/upload", response_class=HTMLResponse)
async def upload_post(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(""),
    user: User = Depends(get_current_user),
):
    default_folder = settings.sharepoint_documents_folder or "Documents"

    if not _sp_configured():
        return templates.TemplateResponse(request, "documents/upload.html", {
            "user": user,
            "default_folder": default_folder,
            "success": None,
            "error": "SharePoint is not configured.",
        })

    target_folder = folder.strip() or default_folder

    try:
        content = await file.read()
        from app.sharepoint import upload_file
        await upload_file(**_sp_kwargs(), folder=target_folder, filename=file.filename, content=content)
        return templates.TemplateResponse(request, "documents/upload.html", {
            "user": user,
            "default_folder": default_folder,
            "success": f"'{file.filename}' uploaded to {target_folder}.",
            "error": None,
        })
    except Exception as e:
        logger.error(f"Document upload failed: {e}")
        return templates.TemplateResponse(request, "documents/upload.html", {
            "user": user,
            "default_folder": default_folder,
            "success": None,
            "error": str(e),
        })


# ---------------------------------------------------------------------------
# Generate (timesheet CSV report)
# ---------------------------------------------------------------------------

@router.get("/generate", response_class=HTMLResponse)
def generate_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    weeks = db.query(Week).order_by(Week.start_date.desc()).limit(24).all()
    users = None
    if user.is_admin:
        users = db.query(User).filter_by(is_approved=True).order_by(User.full_name).all()

    return templates.TemplateResponse(request, "documents/generate.html", {
        "user": user,
        "weeks": weeks,
        "users": users,
    })


@router.post("/generate")
def generate_report(
    week_id: str = Form(...),
    target_user_id: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    uid = target_user_id if (user.is_admin and target_user_id) else user.id
    week = db.get(Week, week_id)
    if not week:
        return HTMLResponse("Week not found", status_code=404)

    entries = (
        db.query(TimesheetEntry)
        .filter_by(user_id=uid, week_id=week_id)
        .join(Assignment)
        .order_by(Assignment.reference_number, TimesheetEntry.task)
        .all()
    )
    target_user = db.get(User, uid)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Week", "Name", "Assignment Ref", "Company", "Task",
        "Mon (h)", "Tue (h)", "Wed (h)", "Thu (h)", "Fri (h)", "Total (h)",
    ])
    week_label = f"{week.start_date} - {week.end_date}"
    name = target_user.full_name if target_user else uid

    for e in entries:
        writer.writerow([
            week_label, name,
            e.assignment.reference_number,
            e.assignment.company_name or "",
            e.task,
            round(e.monday_minutes / 60, 2),
            round(e.tuesday_minutes / 60, 2),
            round(e.wednesday_minutes / 60, 2),
            round(e.thursday_minutes / 60, 2),
            round(e.friday_minutes / 60, 2),
            round(e.total_minutes / 60, 2),
        ])

    filename = f"timesheet_{week.start_date}_{name.replace(' ', '_')}.csv"
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Linked (documents linked to assignments — placeholder)
# ---------------------------------------------------------------------------

@router.get("/linked", response_class=HTMLResponse)
def linked_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assignments = (
        db.query(Assignment)
        .join(Assignment.portfolio_entries)
        .filter_by(user_id=user.id)
        .order_by(Assignment.reference_number)
        .all()
    ) if not user.is_admin else (
        db.query(Assignment).order_by(Assignment.reference_number).all()
    )

    return templates.TemplateResponse(request, "documents/linked.html", {
        "user": user,
        "assignments": assignments,
    })
