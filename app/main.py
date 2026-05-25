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

    # Re-index on every startup so new drives and document changes are always picked up
    try:
        logger.info("Triggering background re-index on startup.")
        from app.indexer import run_indexing_sync
        threading.Thread(target=run_indexing_sync, daemon=True).start()
    except Exception as e:
        logger.error(f"Startup indexing failed: {e}")

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


@app.get("/")
def root():
    return RedirectResponse("/timesheet", status_code=302)
