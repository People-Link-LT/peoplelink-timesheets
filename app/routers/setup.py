from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import User
from app.auth import hash_password
from app.config import settings

router = APIRouter()


@router.post("/setup/create-admin")
def create_admin(email: str, full_name: str, password: str, x_setup_token: str = Header(...)):
    if x_setup_token != settings.setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")
    db: Session = SessionLocal()
    try:
        admin_exists = db.query(User).filter(User.is_admin == True).first()
        if admin_exists:
            return JSONResponse({"error": "Admin already exists"}, status_code=400)
        if db.query(User).filter(User.email == email).first():
            return JSONResponse({"error": "Email already in use"}, status_code=400)
        user = User(
            email=email.lower().strip(),
            full_name=full_name,
            password_hash=hash_password(password),
            is_admin=True,
            is_approved=True,
        )
        db.add(user)
        db.commit()
        return JSONResponse({"ok": True, "email": email})
    finally:
        db.close()


@router.post("/setup/test-backup")
def test_backup(x_setup_token: str = Header(...)):
    if x_setup_token != settings.setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")
    from app.backup import run_backup
    result = run_backup()
    return JSONResponse(result)


@router.post("/setup/test-email")
def test_email(to: str, x_setup_token: str = Header(...)):
    if x_setup_token != settings.setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")
    from app.email import send_otp_email
    try:
        send_otp_email(to, "Test User", "123456")
        return JSONResponse({"ok": True, "message": f"Email sent to {to}"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/setup/test-health-check")
def test_health_check(date: str = "", x_setup_token: str = Header(...)):
    """Run health check. Pass ?date=YYYYMMDD to check a specific date (omit for today).
    Pass ?date=MISSING to simulate a missing-backup alert."""
    if x_setup_token != settings.setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")
    import asyncio
    from app.backup import _check_backup_exists, _send_backup_alert

    if date == "MISSING":
        # Simulate missing backups — test the alert email path
        missing = ["data_MISSING.sql", "system_MISSING.zip"]
        _send_backup_alert("99991231", missing)
        return JSONResponse({"ok": True, "simulated_missing": missing, "alert_sent": True})

    check_date = date or __import__("datetime").date.today().strftime("%Y%m%d")
    missing = asyncio.run(_check_backup_exists(check_date))
    result = {"date": check_date, "missing": missing, "all_present": len(missing) == 0}
    if missing:
        _send_backup_alert(check_date, missing)
        result["alert_sent"] = True
    return JSONResponse(result)


@router.post("/setup/reset-2fa")
def reset_2fa(email: str, x_setup_token: str = Header(...)):
    """Disable 2FA for a user — for recovery only."""
    if x_setup_token != settings.setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)
        user.is_2fa_enabled = False
        user.twofa_method = None
        user.totp_secret = None
        user.email_otp = None
        user.email_otp_expires_at = None
        db.commit()
        return JSONResponse({"ok": True, "email": email, "2fa_reset": True})
    finally:
        db.close()
