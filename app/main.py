import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.database import init_db, get_db, SessionLocal
from app.invenias import fetch_active_assignments
from app.models import Assignment, KnowledgeChunk
from app.routers import auth, timesheet, portfolio, dashboard, admin, profile, setup, documents, ask
from app.scheduler import start_scheduler
from app.templates import templates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db_ready = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_ready
    max_attempts = 10
    for attempt in range(max_attempts):
        try:
            init_db()
            db_ready = True
            break
        except Exception as e:
            if attempt == max_attempts - 1:
                logger.critical(f"DB unreachable after {max_attempts} attempts — running in maintenance mode: {e}")
                break
            wait = min(2 ** attempt, 30)
            logger.warning(f"init_db attempt {attempt + 1}/{max_attempts} failed: {e}. Retrying in {wait}s")
            await asyncio.sleep(wait)
    if db_ready:
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
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


@app.get("/")
def root():
    if not db_ready:
        return RedirectResponse("/demo", status_code=302)
    return RedirectResponse("/timesheet", status_code=302)


@app.get("/demo", response_class=HTMLResponse)
def demo(request: Request):
    return templates.TemplateResponse(request, "demo.html", {})
