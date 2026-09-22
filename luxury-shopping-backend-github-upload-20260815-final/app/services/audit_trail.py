from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MODEL_BY_TABLE
from .api_protection import trusted_client_ip


_request_context: ContextVar[dict[str, str] | None] = ContextVar(
    "audit_request_context",
    default=None,
)


def _text(value: Any, *, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def _client_label(user_agent: str | None) -> str | None:
    agent = (user_agent or "").lower()
    if not agent:
        return None
    platform = (
        "Android" if "android" in agent else "iOS" if any(token in agent for token in ("iphone", "ipad", "ipod"))
        else "Windows" if "windows" in agent else "macOS" if "mac os" in agent or "macintosh" in agent
        else "Linux" if "linux" in agent else "نظام غير معروف"
    )
    browser = (
        "Edge" if "edg/" in agent else "Chrome" if "chrome/" in agent and "edg/" not in agent
        else "Firefox" if "firefox/" in agent else "Safari" if "safari/" in agent and "chrome/" not in agent
        else "متصفح غير معروف"
    )
    device = "جوال" if "mobile" in agent or "android" in agent or "iphone" in agent else "جهاز لوحي" if "ipad" in agent else "كمبيوتر"
    return f"{device} · {platform} · {browser}"


def set_audit_request_context(request: Request) -> Token:
    user_agent = _text(request.headers.get("user-agent"), limit=512)
    metadata = {
        key: value
        for key, value in {
            "ip_address": _text(trusted_client_ip(request), limit=64),
            "user_agent": user_agent,
            "client_label": _client_label(user_agent),
            "request_id": _text(getattr(request.state, "request_id", None), limit=120),
            "request_method": _text(request.method, limit=12),
            "request_path": _text(request.url.path, limit=500),
        }.items()
        if value is not None
    }
    return _request_context.set(metadata)


def reset_audit_request_context(token: Token) -> None:
    _request_context.reset(token)


def request_audit_metadata() -> dict[str, str]:
    return dict(_request_context.get() or {})


def with_request_audit_metadata(extra_data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(extra_data or {})
    for key, value in request_audit_metadata().items():
        payload.setdefault(key, value)
    return payload


def add_audit_log(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    action: str,
    description: str,
    extra_data: dict[str, Any] | None = None,
) -> None:
    model = MODEL_BY_TABLE["audit_logs"]
    session.add(
        model(
            user_id=user_id,
            type=action,
            description=description,
            extra_data=with_request_audit_metadata(extra_data),
        )
    )
