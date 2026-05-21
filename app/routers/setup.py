from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import User
from app.auth import hash_password
import os

router = APIRouter()

SETUP_TOKEN = os.environ.get("SETUP_TOKEN", "")


@router.post("/setup/create-admin")
def create_admin(token: str, email: str, full_name: str, password: str):
    if not SETUP_TOKEN or token != SETUP_TOKEN:
        return JSONResponse({"error": "Invalid token"}, status_code=403)
    db: Session = SessionLocal()
    try:
        if db.query(User).filter(User.email == email).first():
            return JSONResponse({"error": "User already exists"}, status_code=400)
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
