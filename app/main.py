import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import init_db, SessionLocal
from app.scheduler import start_scheduler
from app.invenias import fetch_active_assignments
from app.models import Assignment
from app.routers import auth, timesheet, portfolio, dashboard, admin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Async startup sync
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
    yield


app = FastAPI(title="PeopleLink Timesheets", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router)
app.include_router(timesheet.router)
app.include_router(portfolio.router)
app.include_router(dashboard.router)
app.include_router(admin.router)


@app.get("/")
def root():
    return RedirectResponse("/timesheet", status_code=302)
