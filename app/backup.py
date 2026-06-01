import asyncio
import io
import logging
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import text
from app.config import settings
from app.database import SessionLocal
from app.sharepoint import upload_file

logger = logging.getLogger(__name__)

_APP_ROOT = Path(__file__).parent.parent  # timesheets/
_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
_SKIP_EXTS = {".pyc", ".pyo"}


_SKIP_COLUMNS = {"embedding"}  # pgvector columns — too large and non-portable as SQL strings


def _db_export() -> bytes:
    """Export all tables as SQL INSERT statements using SQLAlchemy."""
    db = SessionLocal()
    try:
        buf = io.StringIO()
        buf.write(f"-- PeopleLink Timesheets backup\n-- {datetime.now(timezone.utc).isoformat()}\n\n")

        tables = db.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )).fetchall()

        for (table,) in tables:
            cols = db.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t AND table_schema = 'public' ORDER BY ordinal_position"
            ), {"t": table}).fetchall()
            # Skip vector columns — pgvector binary data is too large and not portable as SQL
            export_cols = [c[0] for c in cols if c[0] not in _SKIP_COLUMNS]
            if not export_cols:
                continue
            col_list = ", ".join(f'"{c}"' for c in export_cols)
            rows = db.execute(text(f'SELECT {col_list} FROM "{table}"')).fetchall()
            if not rows:
                continue
            buf.write(f'\n-- Table: {table} ({len(rows)} rows)\n')
            for row in rows:
                vals = ", ".join(
                    "NULL" if v is None
                    else f"'{str(v).replace(chr(39), chr(39)+chr(39))}'"
                    for v in row
                )
                buf.write(f'INSERT INTO "{table}" ({col_list}) VALUES ({vals});\n')

        return buf.getvalue().encode("utf-8")
    finally:
        db.close()


def _app_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(_APP_ROOT.rglob("*")):
            if path.is_file():
                if any(part in _SKIP_DIRS for part in path.parts):
                    continue
                if path.suffix in _SKIP_EXTS:
                    continue
                if path.name == ".env":
                    continue
                zf.write(path, path.relative_to(_APP_ROOT))
    return buf.getvalue()


async def _upload(filename: str, content: bytes) -> None:
    await upload_file(
        tenant_id=settings.sharepoint_tenant_id,
        client_id=settings.sharepoint_client_id,
        client_secret=settings.sharepoint_client_secret,
        site_hostname=settings.sharepoint_site_hostname,
        site_path=settings.sharepoint_site_path,
        folder=settings.sharepoint_backup_folder,
        filename=filename,
        content=content,
        drive_name=settings.sharepoint_drive_name,
    )


def run_backup() -> dict:
    """Run backup and return a result dict with 'ok' bool and 'messages' list."""
    messages = []

    if not all([
        settings.sharepoint_tenant_id,
        settings.sharepoint_client_id,
        settings.sharepoint_client_secret,
        settings.sharepoint_site_hostname,
    ]):
        msg = "SharePoint backup skipped: credentials not configured."
        logger.warning(msg)
        return {"ok": False, "messages": [msg]}

    today = date.today().strftime("%Y%m%d")
    ok = True

    try:
        logger.info("Starting database backup…")
        db_bytes = _db_export()
        asyncio.run(_upload(f"data_{today}.sql", db_bytes))
        msg = f"Database backup uploaded ({len(db_bytes):,} bytes)."
        logger.info(msg)
        messages.append(msg)
    except Exception as e:
        ok = False
        msg = f"Database backup failed: {e}"
        logger.error(msg)
        messages.append(msg)

    try:
        logger.info("Starting system (code) backup…")
        zip_bytes = _app_zip()
        asyncio.run(_upload(f"system_{today}.zip", zip_bytes))
        msg = f"System backup uploaded ({len(zip_bytes):,} bytes)."
        logger.info(msg)
        messages.append(msg)
    except Exception as e:
        ok = False
        msg = f"System backup failed: {e}"
        logger.error(msg)
        messages.append(msg)

    return {"ok": ok, "messages": messages}


async def _check_backup_exists(today: str) -> list[str]:
    """Return list of missing backup filenames for today."""
    if not all([settings.sharepoint_tenant_id, settings.sharepoint_client_id,
                settings.sharepoint_client_secret, settings.sharepoint_site_hostname]):
        return []

    token_url = f"https://login.microsoftonline.com/{settings.sharepoint_tenant_id}/oauth2/v2.0/token"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(token_url, data={
            "grant_type": "client_credentials",
            "client_id": settings.sharepoint_client_id,
            "client_secret": settings.sharepoint_client_secret,
            "scope": "https://graph.microsoft.com/.default",
        })
        r.raise_for_status()
        token = r.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        # Resolve site
        site_r = await client.get(
            f"https://graph.microsoft.com/v1.0/sites/{settings.sharepoint_site_hostname}:/{settings.sharepoint_site_path}",
            headers=auth,
        )
        site_r.raise_for_status()
        site_id = site_r.json()["id"]

        # Resolve drive
        if settings.sharepoint_drive_name:
            drives_r = await client.get(
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives", headers=auth
            )
            drives_r.raise_for_status()
            drive = next((d for d in drives_r.json()["value"] if d["name"] == settings.sharepoint_drive_name), None)
            if not drive:
                return [f"Drive '{settings.sharepoint_drive_name}' not found"]
            drive_base = f"https://graph.microsoft.com/v1.0/drives/{drive['id']}"
        else:
            drive_base = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive"

        missing = []
        for fname in [f"data_{today}.sql", f"system_{today}.zip"]:
            chk = await client.get(
                f"{drive_base}/root:/{settings.sharepoint_backup_folder}/{fname}",
                headers=auth,
            )
            if not chk.is_success:
                missing.append(fname)
        return missing


def check_backup_health() -> None:
    """Run at 17:00 — verify today's backups exist; email alert if missing."""
    today = date.today().strftime("%Y%m%d")
    logger.info(f"Backup health check for {today}…")
    try:
        missing = asyncio.run(_check_backup_exists(today))
    except Exception as e:
        logger.error(f"Backup health check error: {e}")
        missing = [f"Health check failed: {e}"]

    if not missing:
        logger.info("Backup health check passed — all files present.")
        return

    logger.warning(f"Backup health check FAILED — missing: {missing}")
    _send_backup_alert(today, missing)


def _send_backup_alert(today: str, missing: list[str]) -> None:
    from app.email import send_backup_alert_email
    recipient = settings.smtp_from or settings.smtp_username
    if not recipient:
        logger.warning("No SMTP_FROM set — cannot send backup alert.")
        return
    try:
        send_backup_alert_email(recipient, today, missing)
        logger.info(f"Backup alert email sent to {recipient}")
    except Exception as e:
        logger.error(f"Failed to send backup alert: {e}")
