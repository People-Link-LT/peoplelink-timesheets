import base64
import io

import pyotp
import qrcode
from app.templates import templates
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/profile")


@router.get("", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "profile.html", {"user": user, "qr": None, "secret": None, "error": None, "success": None})


@router.post("/2fa/setup", response_class=HTMLResponse)
def setup_2fa(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=user.email, issuer_name="PeopleLink Timesheets")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return templates.TemplateResponse(request, "profile.html", {
        "user": user, "qr": qr_b64, "secret": secret, "error": None, "success": None
    })


@router.post("/2fa/enable", response_class=HTMLResponse)
def enable_2fa(
    request: Request,
    code: str = Form(...),
    secret: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    totp = pyotp.TOTP(secret)
    if not totp.verify(code.strip(), valid_window=1):
        img = qrcode.make(totp.provisioning_uri(name=user.email, issuer_name="PeopleLink Timesheets"))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        return templates.TemplateResponse(request, "profile.html", {
            "user": user, "qr": qr_b64, "secret": secret,
            "error": "Invalid code. Make sure your authenticator is synced and try again.", "success": None
        })
    db_user = db.get(User, user.id)
    db_user.totp_secret = secret
    db_user.is_2fa_enabled = True
    db.commit()
    return templates.TemplateResponse(request, "profile.html", {
        "user": db_user, "qr": None, "secret": None, "error": None, "success": "2FA enabled successfully."
    })


@router.post("/2fa/disable", response_class=HTMLResponse)
def disable_2fa(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not user.is_2fa_enabled or not user.totp_secret:
        return RedirectResponse("/profile", status_code=302)
    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(code.strip(), valid_window=1):
        return templates.TemplateResponse(request, "profile.html", {
            "user": user, "qr": None, "secret": None,
            "error": "Invalid code. 2FA was not disabled.", "success": None
        })
    db_user = db.get(User, user.id)
    db_user.is_2fa_enabled = False
    db_user.totp_secret = None
    db.commit()
    return templates.TemplateResponse(request, "profile.html", {
        "user": db_user, "qr": None, "secret": None, "error": None, "success": "2FA disabled."
    })
