from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.auth import get_current_user
from app.models import User, TimesheetEntry, Assignment, Team
from app.weeks import get_or_create_week

router = APIRouter(prefix="/dashboard")
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    week = get_or_create_week(db)

    # Total minutes per assignment this week
    project_totals = (
        db.query(
            Assignment.reference_number,
            Assignment.company_name,
            Assignment.title,
            func.sum(
                TimesheetEntry.monday_minutes + TimesheetEntry.tuesday_minutes
                + TimesheetEntry.wednesday_minutes + TimesheetEntry.thursday_minutes
                + TimesheetEntry.friday_minutes
            ).label("total_minutes"),
        )
        .join(TimesheetEntry, TimesheetEntry.assignment_id == Assignment.id)
        .filter(TimesheetEntry.week_id == week.id)
        .group_by(Assignment.id, Assignment.reference_number, Assignment.company_name, Assignment.title)
        .order_by(func.sum(
            TimesheetEntry.monday_minutes + TimesheetEntry.tuesday_minutes
            + TimesheetEntry.wednesday_minutes + TimesheetEntry.thursday_minutes
            + TimesheetEntry.friday_minutes
        ).desc())
        .all()
    )

    # Per-user totals grouped by team
    user_totals_raw = (
        db.query(
            Team.name.label("team_name"),
            User.full_name,
            User.id.label("user_id"),
            func.coalesce(func.sum(
                TimesheetEntry.monday_minutes + TimesheetEntry.tuesday_minutes
                + TimesheetEntry.wednesday_minutes + TimesheetEntry.thursday_minutes
                + TimesheetEntry.friday_minutes
            ), 0).label("total_minutes"),
        )
        .select_from(User)
        .outerjoin(Team, User.team_id == Team.id)
        .outerjoin(TimesheetEntry, (TimesheetEntry.user_id == User.id) & (TimesheetEntry.week_id == week.id))
        .filter(User.is_approved == True)
        .group_by(Team.name, User.id, User.full_name)
        .order_by(Team.name, User.full_name)
        .all()
    )

    # Group into teams dict
    teams: dict[str, list] = {}
    for row in user_totals_raw:
        team_key = row.team_name or "No Team"
        if team_key not in teams:
            teams[team_key] = []
        teams[team_key].append({
            "name": row.full_name,
            "total_minutes": row.total_minutes,
        })

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "week": week,
        "project_totals": project_totals,
        "teams": teams,
    })
