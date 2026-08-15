from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import HTTPException, status

from ..config import get_settings

try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth
    from firebase_admin import credentials
except Exception:  # pragma: no cover - optional provider dependency
    firebase_admin = None
    firebase_auth = None
    credentials = None


def _inline_credentials_json(settings: Any) -> str:
    return str(
        getattr(settings, "google_application_credentials_json", "")
        or getattr(settings, "firebase_service_account_json", "")
        or ""
    ).strip()


def firebase_admin_configuration_status(settings: Any | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    installed = firebase_admin is not None and firebase_auth is not None and credentials is not None
    has_project_id = bool(str(getattr(settings, "firebase_project_id", "") or "").strip())
    has_inline_credentials = bool(_inline_credentials_json(settings))
    has_file_credentials = bool(str(getattr(settings, "google_application_credentials", "") or "").strip())
    return {
        "provider": "firebase",
        "installed": installed,
        "project_id": has_project_id,
        "inline_credentials": has_inline_credentials,
        "file_credentials": has_file_credentials,
        "configured": bool(installed and has_project_id and (has_inline_credentials or has_file_credentials)),
    }


def ensure_firebase_admin_app(settings: Any | None = None) -> None:
    settings = settings or get_settings()
    provider_status = firebase_admin_configuration_status(settings)
    if not provider_status["installed"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="firebase_admin_not_installed",
        )
    if not provider_status["configured"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="firebase_credentials_required",
        )
    try:
        firebase_admin.get_app()
        return
    except ValueError:
        pass
    try:
        inline_credentials = _inline_credentials_json(settings)
        if inline_credentials:
            credential = credentials.Certificate(json.loads(inline_credentials))
        else:
            credential = credentials.Certificate(str(settings.google_application_credentials).strip())
        firebase_admin.initialize_app(credential, {"projectId": settings.firebase_project_id})
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="firebase_initialization_failed",
        ) from exc


async def verify_firebase_id_token(id_token: str, settings: Any | None = None) -> dict[str, Any]:
    ensure_firebase_admin_app(settings)
    try:
        return await asyncio.to_thread(firebase_auth.verify_id_token, id_token, check_revoked=True)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="firebase_token_invalid") from exc
