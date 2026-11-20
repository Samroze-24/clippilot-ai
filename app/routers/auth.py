"""Auth router: JSON login + form login (Swagger Authorize uses the form one)."""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..auth import create_access_token, verify_password
from ..database import get_db
from ..models import User
from ..schemas import LoginIn, TokenOut, UserOut
from ..auth import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


def _authenticate(db: Session, email: str, password: str) -> User:
    user = db.query(User).filter(User.email == email).first()
    if not user or not user.is_active or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Incorrect email or password")
    return user


def _token(user: User) -> TokenOut:
    return TokenOut(access_token=create_access_token({"sub": user.email}),
                    user_id=user.id, name=user.name, role=user.role)


@router.post("/login", response_model=TokenOut)
def login_json(payload: LoginIn, db: Session = Depends(get_db)):
    return _token(_authenticate(db, payload.email, payload.password))


@router.post("/login-form", response_model=TokenOut)
def login_form(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # OAuth2PasswordRequestForm sends `username` — we treat it as the email.
    return _token(_authenticate(db, form.username, form.password))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
