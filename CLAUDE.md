# PeopleLink Central

Internal hub app for all PeopleLink employees. Live at https://peoplelink-timesheets-production.up.railway.app

## Stack
- **Python / FastAPI** — backend + HTML rendering
- **Jinja2** — HTML templates (in `templates/`)
- **SQLAlchemy + PostgreSQL** — database
- **APScheduler** — scheduled jobs (backup, sync, indexing)
- **Railway** — hosting (project: `ravishing-spontaneity`, service: `peoplelink-timesheets`)
- **Docker / nixpacks** — build (see `Dockerfile`, `nixpacks.toml`)

## Project structure
```
app/
  main.py             # FastAPI app, startup, middleware, routes
  config.py           # All env vars (loaded from .env)
  models.py           # SQLAlchemy models
  database.py         # DB connection + init_db()
  auth.py             # JWT + session logic
  scheduler.py        # Scheduled jobs (backup 02:30, sync 03:00, index 07:00+13:00)
  backup.py           # DB + code backup → SharePoint
  sharepoint.py       # SharePoint / Microsoft Graph API
  invenias.py         # Invenias CRM API
  routers/
    auth.py           # Login, register, 2FA
    timesheet.py      # Timesheet module
    documents.py      # Document Library module
    dashboard.py      # Dashboard
    portfolio.py      # Portfolio module
    admin.py          # Admin panel
    profile.py        # User profile
    ask.py            # Ask PL — AI assistant (RAG)
templates/            # Jinja2 HTML templates
static/               # CSS, JS, images
```

## Modules
- **Timesheet** — `/timesheet` — submit and track working hours
- **Document Library** — `/documents` — browse and upload SharePoint documents
- **Portfolio** — `/portfolio` — employee portfolio
- **Admin** — `/admin` — user management, teams, metadata rules
- **Ask PL** — `/ask` — AI assistant (RAG over SharePoint documents, uses OpenAI embeddings + Claude)

## Deploy workflow
Every change follows this exact sequence:
```
git add <files>
git commit -m "description"
git push origin master
railway up --detach --service peoplelink-timesheets
```
Railway CLI is at `C:\Users\<you>\AppData\Local\railway\railway.exe` if not on PATH.
Live URL: https://peoplelink-timesheets-production.up.railway.app

## GitHub
Repo: https://github.com/People-Link-LT/peoplelink-timesheets  
Org: People-Link-LT  
Branch: `master` (only branch — push directly)

## Environment variables
All config lives in `.env` (never commit this file). See `.env.example` for the full list.
Key vars:
- `DATABASE_URL` — PostgreSQL connection string
- `SECRET_KEY` — JWT signing key
- `INVENIAS_CLIENT_ID/SECRET/USERNAME/PASSWORD` — Invenias CRM auth
- `OPENAI_API_KEY` — embeddings for Ask PL
- `ANTHROPIC_API_KEY` — Claude for Ask PL streaming
- `SHAREPOINT_*` — SharePoint backup + document library

## Scheduled jobs (all automatic, no action needed)
- `02:30 UTC` — backup DB + code to SharePoint
- `03:00 UTC` — sync assignments from Invenias
- `07:00 + 13:00 Vilnius` — re-index SharePoint docs for Ask PL
- `17:00 UTC` — verify backups exist, email alert if missing
- `Sun 03:00 Vilnius` — VACUUM ANALYZE database
- `04:00 Vilnius` — clean up chat messages older than 90 days

## Coding conventions
- Templates use Jinja2 — keep logic in Python, not templates
- All routes return `TemplateResponse` or `JSONResponse`
- DB sessions via `get_db()` dependency or `SessionLocal()` directly in background tasks
- Never commit `.env` — secrets go in Railway environment variables
