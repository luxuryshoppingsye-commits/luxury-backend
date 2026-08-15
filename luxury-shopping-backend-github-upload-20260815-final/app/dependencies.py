from __future__ import annotations

import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .models.domain import User
from .security.tokens import decode_token
from .services.auth_service import account_security_for, roles_for


bearer = HTTPBearer(auto_error=False)


async def optional_user(
    request: Request = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    token = credentials.credentials if credentials is not None else (request.cookies.get("at") if request is not None else None)
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise ValueError("wrong_token_type")
        user_id = uuid.UUID(str(payload["sub"]))
        token_security_version = int(payload.get("sv") or 0)
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_access_token")
    user = await session.get(User, user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="inactive_user")
    state = await account_security_for(session, user.id, create=False)
    account_status = getattr(state, "account_status", "active")
    security_version = int(getattr(state, "security_version", 0) or 0)
    if account_status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account_not_active")
    if security_version != token_security_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="stale_access_token")
    return user


async def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication_required")
    return user


async def user_roles(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> set[str]:
    return set(await roles_for(session, user.id))


def require_roles(*allowed: str) -> Callable:
    async def dependency(
        user: User = Depends(current_user),
        roles: set[str] = Depends(user_roles),
    ) -> User:
        if not roles.intersection(allowed):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient_permissions")
        return user

    return dependency


require_admin = require_roles("admin", "manager")
require_staff = require_roles("admin", "manager", "finance", "logistics", "staff", "employee")
require_partner = require_roles("partner")
require_courier = require_roles("courier", "delivery")
require_marketer = require_roles("marketer")
