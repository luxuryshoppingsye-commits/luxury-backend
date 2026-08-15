from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from ..config import get_settings


ALGORITHM = "HS256"


def create_access_token(user_id: str, roles: list[str], security_version: int = 0) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "roles": roles,
        "type": "access",
        "sv": int(security_version),
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def create_refresh_token() -> tuple[str, str, datetime]:
    settings = get_settings()
    raw = secrets.token_urlsafe(64)
    digest = token_hash(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_days)
    return raw, digest, expires_at


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
