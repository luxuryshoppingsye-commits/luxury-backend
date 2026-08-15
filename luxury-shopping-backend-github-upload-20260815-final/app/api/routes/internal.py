from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...database import get_session
from ...dependencies import require_admin
from ...models.domain import User
from ...services.api_protection import policy_registry_snapshot


router = APIRouter(prefix="/internal", tags=["internal"])


def _database_fingerprint() -> str:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    salt = os.environ.get("RUNTIME_FINGERPRINT_SALT") or settings.jwt_secret[-32:]
    material = f"{parsed.hostname or ''}:{parsed.port or ''}:{settings.database_name}:{salt}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@router.get("/runtime-fingerprint")
async def runtime_fingerprint(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    version_row = (
        await session.execute(
            text(
                "select current_schema() as schema_name, "
                "current_setting('server_version_num') as version_num"
            )
        )
    ).mappings().one()
    alembic = (
        await session.execute(text("select version_num from alembic_version limit 1"))
    ).scalar_one_or_none()
    return {
        "app_env": settings.app_env,
        "api_public_url": settings.api_base_url,
        "app_public_url": settings.app_public_url,
        "database_name": settings.database_name,
        "database_current_schema": version_row["schema_name"],
        "database_major_version": str(version_row["version_num"])[:2],
        "alembic_current_revision": alembic,
        "database_fingerprint_sha256": _database_fingerprint(),
        "release_identifier": os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("RELEASE_ID") or "unknown",
        "backend_deployment_identifier": os.environ.get("RENDER_SERVICE_ID") or "render",
        "server_timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api-protection/policies")
async def api_protection_policies(
    _: User = Depends(require_admin),
):
    return {"policies": policy_registry_snapshot()}
