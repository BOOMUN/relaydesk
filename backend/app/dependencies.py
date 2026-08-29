from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .database import get_db
from .models import User, UserRole
from .security import COOKIE_NAME, user_from_session


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = user_from_session(db, request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user


def require_roles(*roles: UserRole):
    allowed = {role.value for role in roles}

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
        return user

    return dependency
