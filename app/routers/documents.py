import csv
import io
import json as _json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_admin, get_current_user
from app.config import settings
from app.database import get_db
from app.models import Assignment, DocMeta, KnowledgeChunk, TimesheetEntry, User, Week
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
# Category definitions — derived from SharePoint navigation structure
#
# Categories with subcategories: "drive" is empty, "subcategories" lists drives.
# Categories without subcategories: "drive" names the single library directly.
#
# Two drive names are marked TODO — they were truncated in the SharePoint UI.
# Use GET /documents/api/discover-drives to see the exact names and correct them.
# ---------------------------------------------------------------------------

_CATEGORIES = [
    {
        "name": "Aktualūs dokumentai",
        "key":  "cat::Aktualūs dokumentai",
        "drive": "Aktualūs dokumentai",
        "subcategories": [],
        "color_bg":   "bg-blue-50",
        "color_icon": "text-blue-500",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"/>',
    },
    {
        "name": "Veiklos dokumentai",
        "key":  "cat::Veiklos dokumentai",
        "drive": "",
        "subcategories": [
            {"name": "Komunikacija",     "drive": "Komunikacija"},
            {"name": "Skelbimai",        "drive": "Skelbimai"},
            {"name": "Procesai ir KPI",  "drive": "Procesai ir KPI"},
            {"name": "Consulting / Tripod", "drive": "Consulting / Tripod"},
            {"name": "Akademija",        "drive": "Akademija"},
            {"name": "Mokymai",          "drive": "Mokymai"},
            {"name": "Rezultatai",       "drive": "Rezultatai"},
            {"name": "Kiti dokumentai",  "drive": "Kiti dokumentai..."},
            {"name": "Instrukcijos",     "drive": "Instrukcijos"},
        ],
        "color_bg":   "bg-indigo-50",
        "color_icon": "text-indigo-500",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>',
    },
    {
        "name": "Darbuotojų dokumentai",
        "key":  "cat::Darbuotojų dokumentai",
        "drive": "",
        "subcategories": [
            {"name": "Darbo taryba",          "drive": "Darbo taryba"},
            {"name": "Darbų sauga",           "drive": "Darbų sauga"},
            {"name": "Sveikatos patikra",     "drive": "Sveikatos patikra"},
            {"name": "Freelance sutartys",    "drive": "Freelance ir praktikos sutartys"},
            # TODO: verify exact drive name via /documents/api/discover-drives
            {"name": "GDPR darbuotojams",     "drive": "GDPR dokumentai darbuotojams"},
            {"name": "Laikinasis įdarbinimas","drive": "Laikinasis įdarbinimas"},
            # TODO: verify exact drive name via /documents/api/discover-drives
            {"name": "Darbo sutartys",        "drive": "Darbuotojų darbo sutartys, priedai"},
            {"name": "Sveikatos draudimas",   "drive": "Sveikatos draudimas"},
            {"name": "Kiti dokumentai",       "drive": "Kiti dokumentai"},
        ],
        "color_bg":   "bg-green-50",
        "color_icon": "text-green-500",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"/>',
    },
    {
        "name": "Turtas ir jo valdymas",
        "key":  "cat::Turtas ir jo valdymas",
        "drive": "",
        "subcategories": [
            {"name": "IT ir mobilūs įrenginiai", "drive": "IT ir mobilūs įrenginiai"},
            {"name": "Auto ir kuras",             "drive": "Auto ir kuras"},
            {"name": "Biurai",                    "drive": "Biurai"},
            {"name": "Kita",                      "drive": "Kita"},
        ],
        "color_bg":   "bg-amber-50",
        "color_icon": "text-amber-500",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"/>',
    },
    {
        "name": "Admin dokumentai",
        "key":  "cat::Admin dokumentai",
        "drive": "",
        "subcategories": [
            {"name": "Sutartys su tiekėjais", "drive": "Sutartys su tiekėjais"},
            {"name": "BDAR",                  "drive": "Asmens duomenų apsauga"},
            {"name": "Įgaliojimai",           "drive": "Įgaliojimai"},
            {"name": "Įmonės dokumentai",     "drive": "Įmonės dokumentai"},
            {"name": "Įsakymai",              "drive": "Įsakymai"},
            {"name": "Prašymai",              "drive": "Prašymai"},
            {"name": "Konfid. sutartys",      "drive": "Konfidencialumo sutartys"},
            {"name": "Paskolos sutartys",     "drive": "Paskolos sutartys"},
            {"name": "Skolininkai",           "drive": "Skolininkai"},
            {"name": "Kiti dokumentai",       "drive": "Kiti dokumentai."},
        ],
        "color_bg":   "bg-gray-50",
        "color_icon": "text-gray-500",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>',
    },
    {
        "name": "Klientų dokumentai",
        "key":  "cat::Klientų dokumentai",
        "drive": "",
        "subcategories": [
            {"name": "Pasiūlymai",           "drive": "Pasiūlymai"},
            {"name": "Sąskaitos",            "drive": "Sąskaitos"},
            {"name": "Sutartys su klientais","drive": "Sutartys su klientais"},
            {"name": "Kiti dokumentai",      "drive": "Kiti dokumentai.."},
            {"name": "Klientų auginimas",    "drive": "Klientų auginimas"},
        ],
        "color_bg":   "bg-rose-50",
        "color_icon": "text-rose-500",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/>',
    },
    {
        "name": "Consulting projects",
        "key":  "cat::Consulting projects",
        "drive": "",
        "subcategories": [
            {"name": "People Link methods",     "drive": "People Link methods"},
            {"name": "Tripod methods",          "drive": "Tripod methods (surveys)"},
            {"name": "Assessment as a Service", "drive": "Assessment as a Service"},
        ],
        "color_bg":   "bg-purple-50",
        "color_icon": "text-purple-500",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 13.255A23.931 23.931 0 0112 15c-3.183 0-6.22-.62-9-1.745M16 6V4a2 2 0 00-2-2h-4a2 2 0 00-2 2v2m4 6h.01M5 20h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>',
    },
    {
        "name": "Freelanceriams",
        "key":  "cat::Freelanceriams",
        "drive": "",
        "subcategories": [
            {"name": "Rezultatai", "drive": "Rezultatai."},
        ],
        "color_bg":   "bg-teal-50",
        "color_icon": "text-teal-500",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>',
    },
    {
        "name": "Archyvas",
        "key":  "cat::Archyvas",
        "drive": "Archyvas",
        "subcategories": [],
        "color_bg":   "bg-slate-50",
        "color_icon": "text-slate-400",
        "icon_svg": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"/>',
    },
]


def _all_category_drives() -> list[str]:
    """Returns every drive name referenced across all categories and subcategories."""
    drives: set[str] = set()
    for cat in _CATEGORIES:
        if cat.get("drive"):
            drives.add(cat["drive"])
        for sub in cat.get("subcategories", []):
            if sub.get("drive"):
                drives.add(sub["drive"])
    return sorted(drives)


def _category_browse_url(cat: dict) -> str:
    if cat.get("subcategories"):
        return f"/documents/browse?category={cat['name']}"
    drive  = cat.get("drive", "")
    folder = cat.get("folder", "")
    if not drive and not folder:
        return ""
    params = []
    if drive:
        params.append(f"drive={drive}")
    if folder:
        params.append(f"folder={folder}")
    return "/documents/browse?" + "&".join(params)


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def library_home(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cat_keys = [c["key"] for c in _CATEGORIES]
    rows = db.execute(select(DocMeta).where(DocMeta.item_id.in_(cat_keys))).scalars().all()
    metas = {
        r.item_id: {
            "comment":  r.comment or "",
            "audience": _json.loads(r.audience) if r.audience else [],
        }
        for r in rows
    }

    categories = [
        {**cat, "browse_url": _category_browse_url(cat)}
        for cat in _CATEGORIES
    ]

    return templates.TemplateResponse(request, "documents/library.html", {
        "user": user,
        "categories": categories,
        "metas": metas,
    })


@router.get("/browse", response_class=HTMLResponse)
async def browse(
    request: Request,
    folder: str = "",
    drive: str = "",
    category: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Subcategory grid mode — category has child drives
    if category:
        cat = next((c for c in _CATEGORIES if c["name"] == category), None)
        if cat and cat.get("subcategories"):
            sub_keys = [f"subcat::{category}::{s['name']}" for s in cat["subcategories"]]
            rows = db.execute(select(DocMeta).where(DocMeta.item_id.in_(sub_keys))).scalars().all()
            metas = {
                r.item_id: {
                    "comment":  r.comment or "",
                    "audience": _json.loads(r.audience) if r.audience else [],
                }
                for r in rows
            }
            subcategories = [
                {
                    **sub,
                    "key": f"subcat::{category}::{sub['name']}",
                    "browse_url": f"/documents/browse?drive={sub['drive']}",
                    "color_bg":   cat["color_bg"],
                    "color_icon": cat["color_icon"],
                }
                for sub in cat["subcategories"]
            ]
            return templates.TemplateResponse(request, "documents/browse.html", {
                "user": user,
                "subcategories": subcategories,
                "parent_category": cat,
                "current_category": category,
                "files": [],
                "error": None,
                "current_folder": "",
                "current_drive": "",
                "breadcrumb": [],
                "metas": metas,
            })

    # File list mode
    files, error = [], None

    if _sp_configured():
        try:
            if not drive and not folder:
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
                "ai_generated": r.ai_generated,
                "ai_model": r.ai_model or "",
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
        "subcategories": [],
        "parent_category": None,
        "current_category": "",
    })


# ---------------------------------------------------------------------------
# Metadata API
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

    ai_generated = bool(body.get("ai_generated", False))
    ai_model = (body.get("ai_model") or "").strip() or None

    existing = db.execute(select(DocMeta).where(DocMeta.item_id == item_id)).scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if existing:
        existing.comment = comment or None
        existing.audience = _json.dumps(audience) if audience else None
        existing.ai_generated = ai_generated
        existing.ai_model = ai_model
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
            ai_generated=ai_generated,
            ai_model=ai_model,
            updated_by=user.full_name,
            updated_at=now,
        ))
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/meta/generate", response_class=JSONResponse)
async def generate_description(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate an AI description for a document or category using indexed content."""
    body = await request.json()
    item_id = (body.get("item_id") or "").strip()
    name    = (body.get("name") or "").strip()
    drive   = (body.get("drive") or "").strip()

    # Try to find indexed chunks for this item
    chunks = db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.source_id.like(f"{item_id}%"))
        .limit(4)
    ).scalars().all()

    if chunks:
        sample = "\n\n".join(c.content[:500] for c in chunks)
    else:
        sample = f"Document name: {name}\nLibrary: {drive}"

    description, model_used = await _ai_generate_description(name, drive, sample)
    return JSONResponse({"ok": True, "description": description, "model": model_used})


async def _ai_generate_description(name: str, drive: str, sample: str) -> tuple[str, str]:
    prompt = (
        f"You are a document librarian for a Lithuanian executive search firm (People Link).\n"
        f"Write a concise 2-3 sentence description in the same language as the document name "
        f"(Lithuanian if the name is Lithuanian, English if English).\n"
        f"Describe: what this document/library contains, who it is for, and when to use it.\n"
        f"Be specific and practical. Do not start with 'This document'.\n\n"
        f"Library: {drive}\nDocument/folder name: {name}\n\nContent sample:\n{sample}"
    )

    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            msg = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text.strip(), "claude-haiku-4-5"
        except Exception as e:
            logger.error(f"Anthropic description generation failed: {e}")

    if settings.openai_api_key:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.openai_api_key)
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip(), "gpt-4o-mini"
        except Exception as e:
            logger.error(f"OpenAI description generation failed: {e}")

    return "", "none"


# ---------------------------------------------------------------------------
# Admin — discover all drives on the SharePoint site
# ---------------------------------------------------------------------------

@router.get("/api/discover-drives", response_class=JSONResponse)
async def discover_drives(admin: User = Depends(get_current_admin)):
    """Lists every document library on the SharePoint site. Use to verify drive name mappings."""
    if not _sp_configured():
        return JSONResponse({"ok": False, "error": "SharePoint not configured"}, status_code=503)
    try:
        from app.sharepoint import list_drives
        drives = await list_drives(**_sp_base_kwargs())
        return JSONResponse({"ok": True, "drives": drives, "category_drives": _all_category_drives()})
    except Exception as e:
        logger.error(f"Discover drives error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


# ---------------------------------------------------------------------------
# File proxy
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
# Linked
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
