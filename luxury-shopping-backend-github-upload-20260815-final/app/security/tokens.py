from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from ..config import get_settings


ALGORITHM = "HS256"
DEFAULT_AUTH_SESSION_MAX_HOURS = 5


def session_max_age_seconds(settings: Any | None = None) -> int:
    """Return the absolute maximum lifetime for one authenticated session."""
    settings = settings or get_settings()
    configured_hours = int(
        getattr(settings, "auth_session_max_hours", DEFAULT_AUTH_SESSION_MAX_HOURS)
    )
    return max(
        60 * 60,
        min(DEFAULT_AUTH_SESSION_MAX_HOURS * 60 * 60, configured_hours * 60 * 60),
    )


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


def create_password_reset_ticket(user_id: str, reset_id: str, expires_at: datetime) -> str:
    payload = {
        "sub": user_id,
        "rid": reset_id,
        "type": "password_reset_otp",
        "jti": str(uuid.uuid4()),
        "iat": datetime.now(timezone.utc),
        "exp": expires_at,
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=ALGORITHM)


def create_refresh_token() -> tuple[str, str, datetime]:
    settings = get_settings()
    raw = secrets.token_urlsafe(64)
    digest = token_hash(raw)
    configured_lifetime = timedelta(days=settings.jwt_refresh_token_days)
    maximum_lifetime = timedelta(seconds=session_max_age_seconds(settings))
    expires_at = datetime.now(timezone.utc) + min(configured_lifetime, maximum_lifetime)
    return raw, digest, expires_at


def create_session_token() -> tuple[str, str]:
    """Create the durable opaque credential for a remembered app session."""
    raw = secrets.token_urlsafe(64)
    return raw, token_hash(raw)


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
