import random
from datetime import datetime, timezone, timedelta

import pyotp
from app.templates import templates
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_user_2fa_exempt as get_current_user
from app.models import User

router = APIRouter(prefix="/profile")


def _smtp_configured() -> bool:
    from app.config import settings
    return bool(settings.sharepoint_tenant_id and settings.sharepoint_client_id and (settings.smtp_from or settings.smtp_username))


def _ctx(user, **kwargs):
    return {"user": user, "qr": None, "secret": None, "error": None, "success": None, "setup_method": None, "smtp_configured": _smtp_configured(), **kwargs}


def _send_email_otp(user: User, db: Session, background_tasks: BackgroundTasks) -> None:
    from app.email import send_otp_email
    code = str(random.randint(100000, 999999))
    user.email_otp = code
    user.email_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.commit()
    background_tasks.add_task(send_otp_email, user.email, user.full_name, code)


@router.get("", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "profile.html", _ctx(user))


@router.post("/2fa/setup", response_class=HTMLResponse)
def setup_2fa(
    request: Request,
    background_tasks: BackgroundTasks,
    method: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if method == "totp":
        secret = pyotp.random_base32()
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="PeopleLink Timesheets")
        return templates.TemplateResponse(request, "profile.html", _ctx(user, qr=uri, secret=secret, setup_method="totp"))

    elif method == "email":
        _send_email_otp(db.get(User, user.id), db, background_tasks)
        return templates.TemplateResponse(request, "profile.html", _ctx(user, setup_method="email"))


@router.post("/2fa/enable", response_class=HTMLResponse)
def enable_2fa(
    request: Request,
    code: str = Form(...),
    method: str = Form(...),
    secret: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    db_user = db.get(User, user.id)
    valid = False

    if method == "totp" and secret:
        valid = pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
        if valid:
            db_user.totp_secret = secret
    elif method == "email" and db_user.email_otp:
        now = datetime.now(timezone.utc)
        expires = db_user.email_otp_expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        valid = code.strip() == db_user.email_otp and now < expires
        if valid:
            db_user.email_otp = None
            db_user.email_otp_expires_at = None

    if not valid:
        if method == "totp" and secret:
            uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="PeopleLink Timesheets")
            return templates.TemplateResponse(request, "profile.html", _ctx(
                user, qr=uri, secret=secret, setup_method="totp",
                error="Invalid code. Make sure your authenticator is synced and try again."
            ))
        return templates.TemplateResponse(request, "profile.html", _ctx(
            user, setup_method=method, error="Invalid or expired code. Try again."
        ))

    db_user.is_2fa_enabled = True
    db_user.twofa_method = method
    db.commit()
    return templates.TemplateResponse(request, "profile.html", _ctx(db_user, success="2FA enabled successfully."))


@router.post("/2fa/disable", response_class=HTMLResponse)
def disable_2fa(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    db_user = db.get(User, user.id)
    if not db_user.is_2fa_enabled:
        return RedirectResponse("/profile", status_code=302)

    method = db_user.twofa_method or "totp"
    valid = False

    if method == "totp" and db_user.totp_secret:
        valid = pyotp.TOTP(db_user.totp_secret).verify(code.strip(), valid_window=1)
    elif method == "email" and db_user.email_otp:
        now = datetime.now(timezone.utc)
        expires = db_user.email_otp_expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        valid = code.strip() == db_user.email_otp and now < expires

    if not valid:
        return templates.TemplateResponse(request, "profile.html", _ctx(
            db_user, error="Invalid code. 2FA was not disabled."
        ))

    db_user.is_2fa_enabled = False
    db_user.twofa_method = None
    db_user.totp_secret = None
    db_user.email_otp = None
    db_user.email_otp_expires_at = None
    db.commit()
    return templates.TemplateResponse(request, "profile.html", _ctx(db_user, success="2FA has been disabled."))


@router.post("/2fa/send-code", response_class=HTMLResponse)
def send_disable_code(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    db_user = db.get(User, user.id)
    if db_user.is_2fa_enabled and db_user.twofa_method == "email":
        _send_email_otp(db_user, db, background_tasks)
        return templates.TemplateResponse(request, "profile.html", _ctx(
            db_user, success="A code has been sent to your email."
        ))
    return RedirectResponse("/profile", status_code=302)
