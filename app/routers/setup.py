from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import User
from app.auth import hash_password

router = APIRouter()


@router.post("/setup/create-admin")
def create_admin(email: str, full_name: str, password: str):
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
