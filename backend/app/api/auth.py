from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import get_current_user
from ..models import User
from ..schemas import BootstrapResponse, IntegrationStatus, LoginRequest, UserResponse
from ..security import COOKIE_NAME, create_session, delete_session, verify_password


router = APIRouter(prefix="/api/auth", tags=["auth"])


def serialize_user(user: User) -> UserResponse:
    return UserResponse.model_validate(user)


@router.post("/login", response_model=BootstrapResponse)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    token, expires_at = create_session(db, user)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
        expires=expires_at,
        path="/",
    )
    return bootstrap_payload(user)


@router.get("/me", response_model=BootstrapResponse)
def me(user: User = Depends(get_current_user)):
    return bootstrap_payload(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    delete_session(db, request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")


def bootstrap_payload(user: User) -> BootstrapResponse:
    return BootstrapResponse(
        app_name=settings.app_name,
        user=serialize_user(user),
        integration=IntegrationStatus(
            openai=settings.openai_enabled,
            whatsapp=settings.whatsapp_enabled,
            whatsapp_provider=settings.whatsapp_provider,
            mode="live" if settings.whatsapp_enabled else "demo",
        ),
    )
