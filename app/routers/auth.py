from app.templates import templates
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.auth import (
    hash_password, verify_password, create_access_token,
    create_2fa_pending_token, verify_2fa_pending_token,
    get_optional_user, COOKIE_NAME, COOKIE_2FA
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

    if user.is_2fa_enabled:
        pending_token = create_2fa_pending_token(user.id)
        response = RedirectResponse("/login/2fa", status_code=302)
        response.set_cookie(COOKIE_2FA, pending_token, httponly=True, samesite="lax", secure=True, max_age=300)
        return response

    token = create_access_token(user.id)
    response = RedirectResponse("/timesheet", status_code=302)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", secure=True, max_age=60 * 60 * 8)
    return response


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request):
    pending = request.cookies.get(COOKIE_2FA)
    if not pending or not verify_2fa_pending_token(pending):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "login_2fa.html", {"error": None})


@router.post("/login/2fa", response_class=HTMLResponse)
def login_2fa_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    import pyotp
    pending = request.cookies.get(COOKIE_2FA)
    user_id = verify_2fa_pending_token(pending) if pending else None
    if not user_id:
        return RedirectResponse("/login", status_code=302)

    user = db.get(User, user_id)
    if not user or not user.totp_secret:
        return RedirectResponse("/login", status_code=302)

    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(code.strip(), valid_window=1):
        return templates.TemplateResponse(request, "login_2fa.html", {"error": "Invalid code. Try again."})

    token = create_access_token(user.id)
    response = RedirectResponse("/timesheet", status_code=302)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", secure=True, max_age=60 * 60 * 8)
    response.delete_cookie(COOKIE_2FA)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(COOKIE_2FA)
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
