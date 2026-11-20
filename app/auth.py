"""JWT auth + password hashing (direct bcrypt, no passlib) + API-key auth."""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import bcrypt
from sqlalchemy.orm import Session

from .database import get_db
from .models import ApiKey, User

SECRET_KEY = os.getenv("SECRET_KEY", "clippilot-dev-secret-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

# Swagger Authorize sends form data, so the token URL MUST be the form endpoint.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login-form", auto_error=False)


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def _user_from_token(token: str, db: Session) -> User | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str | None = payload.get("sub")
        if email is None:
            return None
    except JWTError:
        return None
    user = db.query(User).filter(User.email == email).first()
    return user if user and user.is_active else None


def _user_from_api_key(key: str, db: Session) -> User | None:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    record = db.query(ApiKey).filter(ApiKey.key_hash == digest, ApiKey.is_active == True).first()  # noqa: E712
    if record is None:
        return None
    record.last_used_at = datetime.utcnow()
    db.commit()
    user = db.query(User).filter(User.id == record.user_id).first()
    return user if user and user.is_active else None


def new_api_key() -> str:
    return "cp_" + secrets.token_hex(24)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def get_current_principal(request: Request, db: Session = Depends(get_db)) -> User:
    """Accepts either a Bearer JWT (dashboard login) or an X-API-Key header
    (local API keys issued on the API keys page)."""
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    api_key = request.headers.get("X-API-Key")
    if api_key:
        user = _user_from_api_key(api_key, db)
        if user is None:
            raise credentials_exc
        return user
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user = _user_from_token(auth[7:], db)
        if user is None:
            raise credentials_exc
        return user
    raise credentials_exc


async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exc
    user = _user_from_token(token, db)
    if user is None:
        raise credentials_exc
    return user
