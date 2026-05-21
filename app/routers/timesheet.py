from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_user
from app.models import User, TimesheetEntry, Assignment, TASK_CHOICES
from app.weeks import get_or_create_week, get_week_by_offset

router = APIRouter(prefix="/timesheet")
templates = Jinja2Templates(directory="templates")

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def _get_entries(db: Session, user_id: str, week_id: str) -> list[TimesheetEntry]:
    return (
        db.query(TimesheetEntry)
        .filter_by(user_id=user_id, week_id=week_id)
        .join(Assignment)
        .order_by(Assignment.reference_number, TimesheetEntry.task)
        .all()
    )


@router.get("", response_class=HTMLResponse)
def timesheet_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    week = get_or_create_week(db)
    entries = _get_entries(db, user.id, week.id)
    portfolio_assignments = [p.assignment for p in user.portfolio]
    return templates.TemplateResponse("timesheet.html", {
        "request": request,
        "user": user,
        "week": week,
        "entries": entries,
        "task_choices": TASK_CHOICES,
        "days": DAYS,
        "portfolio_assignments": portfolio_assignments,
    })


@router.post("/rows/add", response_class=HTMLResponse)
def add_row(
    request: Request,
    assignment_id: str = Form(...),
    task: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if task not in TASK_CHOICES:
        raise HTTPException(400, "Invalid task")
    week = get_or_create_week(db)

    existing = db.query(TimesheetEntry).filter_by(
        user_id=user.id, week_id=week.id, assignment_id=assignment_id, task=task
    ).first()
    if existing:
        entry = existing
    else:
        entry = TimesheetEntry(
            user_id=user.id, week_id=week.id, assignment_id=assignment_id, task=task
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)

    return templates.TemplateResponse("partials/entry_row.html", {
        "request": request,
        "entry": entry,
        "days": DAYS,
    })


@router.put("/entries/{entry_id}", response_class=HTMLResponse)
def update_entry(
    entry_id: str,
    request: Request,
    day: str = Form(...),
    minutes: int = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.query(TimesheetEntry).filter_by(id=entry_id, user_id=user.id).first()
    if not entry:
        raise HTTPException(404)
    if day not in DAYS:
        raise HTTPException(400, "Invalid day")
    setattr(entry, f"{day}_minutes", max(0, minutes))
    db.commit()
    db.refresh(entry)
    return templates.TemplateResponse("partials/entry_cell.html", {
        "request": request,
        "entry": entry,
        "day": day,
    })


@router.delete("/entries/{entry_id}", response_class=HTMLResponse)
def delete_entry(
    entry_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.query(TimesheetEntry).filter_by(id=entry_id, user_id=user.id).first()
    if entry:
        db.delete(entry)
        db.commit()
    return HTMLResponse("")


@router.post("/copy-last-week", response_class=HTMLResponse)
def copy_last_week(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    current_week = get_or_create_week(db)
    last_week = get_week_by_offset(db, offset=-1)
    if not last_week:
        entries = _get_entries(db, user.id, current_week.id)
        return templates.TemplateResponse("partials/entries_tbody.html", {
            "request": request, "entries": entries, "days": DAYS,
            "notice": "No data found from last week."
        })

    last_entries = _get_entries(db, user.id, last_week.id)
    for old in last_entries:
        exists = db.query(TimesheetEntry).filter_by(
            user_id=user.id, week_id=current_week.id,
            assignment_id=old.assignment_id, task=old.task
        ).first()
        if not exists:
            db.add(TimesheetEntry(
                user_id=user.id,
                week_id=current_week.id,
                assignment_id=old.assignment_id,
                task=old.task,
                monday_minutes=old.monday_minutes,
                tuesday_minutes=old.tuesday_minutes,
                wednesday_minutes=old.wednesday_minutes,
                thursday_minutes=old.thursday_minutes,
                friday_minutes=old.friday_minutes,
            ))
    db.commit()

    entries = _get_entries(db, user.id, current_week.id)
    return templates.TemplateResponse("partials/entries_tbody.html", {
        "request": request, "entries": entries, "days": DAYS,
        "notice": f"Copied {len(last_entries)} rows from last week."
    })
