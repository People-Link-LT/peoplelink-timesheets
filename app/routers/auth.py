import random
from datetime import datetime, timezone, timedelta

import pyotp
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


def _set_auth_cookie(response, user_id: str):
    token = create_access_token(user_id)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", secure=True, max_age=60 * 60 * 8)


def _send_email_otp(user: User, db: Session) -> bool:
    from app.email import send_otp_email
    code = str(random.randint(100000, 999999))
    user.email_otp = code
    user.email_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.commit()
    try:
        send_otp_email(user.email, user.full_name, code)
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to send OTP email: {e}")
        return False


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
        method = user.twofa_method or "totp"
        if method == "email":
            _send_email_otp(user, db)
        pending_token = create_2fa_pending_token(user.id, method)
        response = RedirectResponse("/login/2fa", status_code=302)
        response.set_cookie(COOKIE_2FA, pending_token, httponly=True, samesite="lax", secure=True, max_age=600)
        return response

    response = RedirectResponse("/timesheet", status_code=302)
    _set_auth_cookie(response, user.id)
    return response


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, db: Session = Depends(get_db)):
    pending = request.cookies.get(COOKIE_2FA)
    info = verify_2fa_pending_token(pending) if pending else None
    if not info:
        return RedirectResponse("/login", status_code=302)
    user = db.get(User, info["user_id"])
    return templates.TemplateResponse(request, "login_2fa.html", {
        "error": None, "method": info["method"],
        "email_hint": user.email[:3] + "***" + user.email[user.email.index("@"):] if user else "",
    })


@router.post("/login/2fa", response_class=HTMLResponse)
def login_2fa_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    pending = request.cookies.get(COOKIE_2FA)
    info = verify_2fa_pending_token(pending) if pending else None
    if not info:
        return RedirectResponse("/login", status_code=302)

    user = db.get(User, info["user_id"])
    if not user:
        return RedirectResponse("/login", status_code=302)

    method = info["method"]
    valid = False

    if method == "totp" and user.totp_secret:
        valid = pyotp.TOTP(user.totp_secret).verify(code.strip(), valid_window=1)
    elif method == "email" and user.email_otp:
        now = datetime.now(timezone.utc)
        expires = user.email_otp_expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        valid = code.strip() == user.email_otp and now < expires

    if not valid:
        email_hint = user.email[:3] + "***" + user.email[user.email.index("@"):]
        return templates.TemplateResponse(request, "login_2fa.html", {
            "error": "Invalid or expired code. Try again.",
            "method": method, "email_hint": email_hint,
        })

    if method == "email":
        user.email_otp = None
        user.email_otp_expires_at = None
        db.commit()

    response = RedirectResponse("/timesheet", status_code=302)
    _set_auth_cookie(response, user.id)
    response.delete_cookie(COOKIE_2FA)
    return response


@router.post("/login/2fa/resend", response_class=HTMLResponse)
def login_2fa_resend(request: Request, db: Session = Depends(get_db)):
    pending = request.cookies.get(COOKIE_2FA)
    info = verify_2fa_pending_token(pending) if pending else None
    if not info or info["method"] != "email":
        return RedirectResponse("/login", status_code=302)
    user = db.get(User, info["user_id"])
    if user:
        _send_email_otp(user, db)
    email_hint = user.email[:3] + "***" + user.email[user.email.index("@"):] if user else ""
    return templates.TemplateResponse(request, "login_2fa.html", {
        "error": None, "method": "email", "email_hint": email_hint,
        "resent": True,
    })


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
