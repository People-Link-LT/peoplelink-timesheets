import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.database import init_db, SessionLocal
from app.invenias import fetch_active_assignments
from app.models import Assignment, KnowledgeChunk
from app.routers import auth, timesheet, portfolio, dashboard, admin, profile, setup, documents, ask
from app.scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _catchup_backup() -> None:
    """Run on startup: if today's backup is missing (service was down at 02:30), create it now."""
    from datetime import date, timezone as tz
    from datetime import datetime as dt
    from app.backup import run_backup, _check_backup_exists

    now_utc = dt.now(tz.utc)
    # Only catch up if it's already past the scheduled backup window (02:30 UTC)
    if now_utc.hour < 3:
        return
    today = date.today().strftime("%Y%m%d")
    try:
        missing = asyncio.run(_check_backup_exists(today))
    except Exception as e:
        logger.warning(f"Startup backup-check failed: {e}")
        return
    if missing:
        logger.info(f"Startup catch-up: today's backup missing ({missing}), running now…")
        run_backup()
    else:
        logger.info("Startup backup-check passed — today's backup already exists.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        items = await fetch_active_assignments()
        db = SessionLocal()
        now = datetime.now(timezone.utc)
        for item in items:
            item_id = item.get("ItemId")
            if not item_id:
                continue
            existing = db.get(Assignment, item_id)
            if existing:
                existing.reference_number = item.get("AssignmentReferenceNumber", existing.reference_number)
                existing.company_name = item.get("CompanyDisplayName", existing.company_name)
                existing.title = item.get("FileAs", existing.title)
                existing.last_synced_at = now
            else:
                db.add(Assignment(
                    id=item_id,
                    reference_number=item.get("AssignmentReferenceNumber", ""),
                    company_name=item.get("CompanyDisplayName", ""),
                    title=item.get("FileAs", ""),
                    status="Active",
                    last_synced_at=now,
                ))
        db.commit()
        db.close()
        logger.info(f"Startup sync: {len(items)} assignments loaded.")
    except Exception as e:
        logger.error(f"Startup Invenias sync failed: {e}")
    start_scheduler()
    threading.Thread(target=_catchup_backup, daemon=True, name="catchup-backup").start()

    # Indexing runs on schedule (07:00 + 13:00 Vilnius) and via the admin
    # "Re-index" button — not on startup, to avoid worker starvation on boot.

    yield


app = FastAPI(title="PeopleLink Central", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router)
app.include_router(timesheet.router)
app.include_router(portfolio.router)
app.include_router(dashboard.router)
app.include_router(admin.router)
app.include_router(profile.router)
app.include_router(setup.router)
app.include_router(documents.router)
app.include_router(ask.router)


@app.middleware("http")
async def security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return RedirectResponse("/timesheet", status_code=302)
