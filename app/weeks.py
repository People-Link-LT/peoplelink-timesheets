from datetime import date, timedelta
from sqlalchemy.orm import Session
from app.models import Week


def get_week_start(d: date) -> date:
    """Return the Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def get_or_create_week(db: Session, d: date | None = None) -> Week:
    if d is None:
        d = date.today()
    start = get_week_start(d)
    end = start + timedelta(days=4)

    week = db.query(Week).filter(Week.start_date == start).first()
    if not week:
        week = Week(start_date=start, end_date=end)
        db.add(week)
        db.commit()
        db.refresh(week)
    return week


def get_week_by_offset(db: Session, offset: int = 0) -> Week | None:
    """offset=0 → current week, offset=-1 → last week."""
    d = date.today() + timedelta(weeks=offset)
    start = get_week_start(d)
    return db.query(Week).filter(Week.start_date == start).first()
