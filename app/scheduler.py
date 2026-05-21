import asyncio
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Assignment
from app.invenias import fetch_active_assignments

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def sync_assignments():
    logger.info("Syncing Invenias assignments…")
    try:
        items = asyncio.run(fetch_active_assignments())
    except Exception as e:
        logger.error(f"Invenias sync failed: {e}")
        return

    db: Session = SessionLocal()
    try:
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
                existing.status = item.get("Status_lookup", existing.status)
                existing.last_synced_at = now
            else:
                db.add(Assignment(
                    id=item_id,
                    reference_number=item.get("AssignmentReferenceNumber", ""),
                    company_name=item.get("CompanyDisplayName", ""),
                    title=item.get("FileAs", ""),
                    status=item.get("Status_lookup", "Active"),
                    last_synced_at=now,
                ))
        db.commit()
        logger.info(f"Synced {len(items)} assignments.")
    except Exception as e:
        db.rollback()
        logger.error(f"DB error during assignment sync: {e}")
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(sync_assignments, "cron", hour=3, minute=0, id="invenias_sync", replace_existing=True)
    from app.backup import run_backup
    scheduler.add_job(run_backup, "cron", hour=2, minute=30, id="sharepoint_backup", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started (Invenias sync 03:00, SharePoint backup 02:30).")
