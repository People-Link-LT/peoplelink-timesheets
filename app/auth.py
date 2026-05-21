from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt
from jose import JWTError, jwt
from fastapi import Request, HTTPException, status, Depends
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User

COOKIE_NAME = "ts_token"
COOKIE_2FA = "ts_2fa_pending"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.secret_key, algorithm=settings.algorithm)


def create_2fa_pending_token(user_id: str, method: str = "totp") -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=10)
    return jwt.encode({"sub": user_id, "type": "2fa", "method": method, "exp": expire}, settings.secret_key, algorithm=settings.algorithm)


def verify_2fa_pending_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        if payload.get("type") != "2fa":
            return None
        return {"user_id": payload.get("sub"), "method": payload.get("method", "totp")}
    except JWTError:
        return None


def _get_user_from_request(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id: str = payload.get("sub")
        if not user_id:
            return None
    except JWTError:
        return None
    return db.get(User, user_id)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = _get_user_from_request(request, db)
    if not user or not user.is_approved:
        raise HTTPException(status_code=status.HTTP_302_FOUND, headers={"Location": "/login"})
    if not user.is_2fa_enabled:
        raise HTTPException(status_code=status.HTTP_302_FOUND, headers={"Location": "/profile?setup_2fa=1"})
    return user


def get_current_user_2fa_exempt(request: Request, db: Session = Depends(get_db)) -> User:
    """For profile/setup routes — only requires login and approval, not 2FA."""
    user = _get_user_from_request(request, db)
    if not user or not user.is_approved:
        raise HTTPException(status_code=status.HTTP_302_FOUND, headers={"Location": "/login"})
    return user


def get_current_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    return _get_user_from_request(request, db)
