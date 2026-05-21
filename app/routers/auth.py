from app.templates import templates
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.auth import (
    hash_password, verify_password, create_access_token,
    get_optional_user, COOKIE_NAME
)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_optional_user(request, db)
    if user and user.is_approved:
        return RedirectResponse("/timesheet", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email.lower().strip()).first()
    error = None
    if not user or not verify_password(password, user.password_hash):
        error = "Invalid email or password."
    elif not user.is_approved:
        error = "Your account is pending admin approval."

    if error:
        return templates.TemplateResponse(request, "login.html", {"error": error})

    token = create_access_token(user.id)
    response = RedirectResponse("/timesheet", status_code=302)
    response.set_cookie(
        COOKIE_NAME, token,
        httponly=True, samesite="lax", secure=True,
        max_age=60 * 60 * 8,
    )
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "An account with this email already exists."}
        )
    user = User(email=email, full_name=full_name, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return templates.TemplateResponse(request, "register.html", {"error": None, "success": True})
