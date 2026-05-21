import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import init_db
from app.scheduler import start_scheduler, sync_assignments
from app.routers import auth, timesheet, portfolio, dashboard, admin

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sync_assignments()       # sync on startup
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
