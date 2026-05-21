import asyncio
import io
import logging
import os
import subprocess
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings
from app.sharepoint import upload_file

logger = logging.getLogger(__name__)

_APP_ROOT = Path(__file__).parent.parent  # timesheets/
_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
_SKIP_EXTS = {".pyc", ".pyo"}


def _pg_dump() -> bytes:
    url = urlparse(settings.database_url)
    env = os.environ.copy()
    env["PGPASSWORD"] = url.password or ""
    result = subprocess.run(
        [
            "pg_dump",
            "-h", url.hostname,
            "-p", str(url.port or 5432),
            "-U", url.username,
            url.path.lstrip("/"),
            "--no-owner",
            "--no-acl",
            "--clean",
            "--if-exists",
        ],
        capture_output=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode()}")
    return result.stdout


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
    errors = []

    try:
        logger.info("Starting database backup…")
        db_bytes = _pg_dump()
        asyncio.run(_upload(f"data_{today}.sql", db_bytes))
        logger.info(f"Database backup uploaded ({len(db_bytes):,} bytes).")
    except Exception as e:
        logger.error(f"Database backup failed: {e}")
        errors.append(str(e))

    try:
        logger.info("Starting system (code) backup…")
        zip_bytes = _app_zip()
        asyncio.run(_upload(f"system_{today}.zip", zip_bytes))
        logger.info(f"System backup uploaded ({len(zip_bytes):,} bytes).")
    except Exception as e:
        logger.error(f"System backup failed: {e}")
        errors.append(str(e))

    if not errors:
        logger.info("Daily backup to SharePoint completed successfully.")
