from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .models import User, UserSession


COOKIE_NAME = "agentdesk_session"
password_hash = PasswordHash.recommended()


def hash_password(value: str) -> str:
    return password_hash.hash(value)


def verify_password(value: str, encoded: str) -> bool:
    return password_hash.verify(value, encoded)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)
    db.add(UserSession(token_hash=token_digest(token), user_id=user.id, expires_at=expires_at))
    db.commit()
    return token, expires_at


def delete_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == token_digest(token)))
        db.commit()


def user_from_session(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    record = db.scalar(select(UserSession).where(UserSession.token_hash == token_digest(token)))
    if record is None:
        return None
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        db.delete(record)
        db.commit()
        return None
    user = db.get(User, record.user_id)
    return user if user and user.is_active else None
