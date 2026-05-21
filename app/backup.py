import asyncio
import io
import logging
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import text
from app.config import settings
from app.database import SessionLocal
from app.sharepoint import upload_file

logger = logging.getLogger(__name__)

_APP_ROOT = Path(__file__).parent.parent  # timesheets/
_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
_SKIP_EXTS = {".pyc", ".pyo"}


def _db_export() -> bytes:
    """Export all tables as SQL INSERT statements using SQLAlchemy."""
    db = SessionLocal()
    try:
        buf = io.StringIO()
        buf.write(f"-- PeopleLink Timesheets backup\n-- {datetime.now(timezone.utc).isoformat()}\n\n")

        # Get all table names in dependency order
        tables = db.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )).fetchall()

        for (table,) in tables:
            rows = db.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            if not rows:
                continue
            cols = db.execute(text(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = :t AND table_schema = 'public' ORDER BY ordinal_position"
            ), {"t": table}).fetchall()
            col_names = ", ".join(f'"{c[0]}"' for c in cols)
            buf.write(f'\n-- Table: {table} ({len(rows)} rows)\n')
            for row in rows:
                vals = ", ".join(
                    "NULL" if v is None
                    else f"'{str(v).replace(chr(39), chr(39)+chr(39))}'"
                    for v in row
                )
                buf.write(f'INSERT INTO "{table}" ({col_names}) VALUES ({vals});\n')

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
    )


def run_backup() -> None:
    if not all([
        settings.sharepoint_tenant_id,
        settings.sharepoint_client_id,
        settings.sharepoint_client_secret,
        settings.sharepoint_site_hostname,
    ]):
        logger.warning("SharePoint backup skipped: credentials not configured.")
        return

    today = date.today().strftime("%Y%m%d")

    try:
        logger.info("Starting database backup…")
        db_bytes = _db_export()
        asyncio.run(_upload(f"data_{today}.sql", db_bytes))
        logger.info(f"Database backup uploaded ({len(db_bytes):,} bytes).")
    except Exception as e:
        logger.error(f"Database backup failed: {e}")

    try:
        logger.info("Starting system (code) backup…")
        zip_bytes = _app_zip()
        asyncio.run(_upload(f"system_{today}.zip", zip_bytes))
        logger.info(f"System backup uploaded ({len(zip_bytes):,} bytes).")
    except Exception as e:
        logger.error(f"System backup failed: {e}")
