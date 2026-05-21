from app.templates import templates
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_admin, hash_password
from app.models import User, Team
from app.scheduler import sync_assignments

router = APIRouter(prefix="/admin")


@router.get("/users", response_class=HTMLResponse)
def admin_users(request: Request, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    users = db.query(User).order_by(User.full_name).all()
    teams = db.query(Team).order_by(Team.name).all()
    return templates.TemplateResponse(request, "admin/users.html", {
        "user": admin, "users": users, "teams": teams
    })


@router.post("/users/create")
def create_user(
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    team_id: str = Form(""),
    is_admin: str = Form("off"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    u = User(
        email=email.lower().strip(),
        full_name=full_name,
        password_hash=hash_password(password),
        team_id=team_id or None,
        is_admin=(is_admin == "on"),
        is_approved=True,
    )
    db.add(u)
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/approve")
def approve_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    u = db.get(User, user_id)
    if u:
        u.is_approved = True
        db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/reject")
def reject_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    u = db.get(User, user_id)
    if u and not u.is_admin:
        db.delete(u)
        db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/update")
def update_user(
    user_id: str,
    full_name: str = Form(...),
    team_id: str = Form(""),
    is_admin: str = Form("off"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    u = db.get(User, user_id)
    if u:
        u.full_name = full_name
        u.team_id = team_id or None
        u.is_admin = (is_admin == "on")
        db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/delete")
def delete_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    u = db.get(User, user_id)
    if u and u.id != admin.id:
        db.delete(u)
        db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.get("/teams", response_class=HTMLResponse)
def admin_teams(request: Request, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    teams = db.query(Team).order_by(Team.name).all()
    return templates.TemplateResponse(request, "admin/teams.html", {
        "user": admin, "teams": teams
    })


@router.post("/teams/create")
def create_team(name: str = Form(...), db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    db.add(Team(name=name.strip()))
    db.commit()
    return RedirectResponse("/admin/teams", status_code=302)


@router.post("/teams/{team_id}/delete")
def delete_team(team_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    t = db.get(Team, team_id)
    if t:
        db.delete(t)
        db.commit()
    return RedirectResponse("/admin/teams", status_code=302)


@router.post("/sync-now")
def manual_sync(admin: User = Depends(get_current_admin)):
    sync_assignments()
    return RedirectResponse("/admin/users", status_code=302)
