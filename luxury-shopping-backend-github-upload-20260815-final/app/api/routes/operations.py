from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import BACKEND_DIR, get_settings
from ...database import SessionFactory, get_session
from ...dependencies import bearer, current_user, optional_user, require_admin, require_courier, require_marketer, require_partner, require_staff, user_roles
from ...models import MODEL_BY_TABLE, RESOURCE_TABLES
from ...models.domain import FileAsset, Order, OrderItem, Product, ProductVariant, Profile, User, UserRole
from ...repositories.resources import ResourceRepository, serialize_record
from ...security.tokens import decode_token
from ...services.auth_service import (
    _canonical_role,
    _roles_from_any,
    account_security_for,
    auth_payload,
    bump_security_version,
    create_user,
    revoke_all_refresh_tokens,
    roles_for,
)
from ...services.staff_permissions import require_staff_permission
from ...services.store_review_compat import fetch_public_store_reviews, fetch_user_store_review
from ...services.catalog_policy import (
    build_public_product_rows,
    new_product_clause,
    normalize_product_mutation_values,
    public_product_base_clauses,
    public_product_clauses,
    public_main_storefront_response,
    public_storefront_response,
    validate_public_product_or_404,
)
from ...services.category_integrity import (
    create_category_record,
    soft_delete_category_record,
    update_category_record,
)
from ...services.function_service import execute_function, execute_public_ai_chat
from ...services.firebase_auth_service import firebase_admin_configuration_status
from ...services.image_pipeline import prepare_image_upload
from ...services.financial_calculator import (
    advisory_xact_lock,
    approved_payment_total,
    financial_response_row,
    find_idempotent_refund,
    money,
    receipt_amount_for_order,
    refunded_total,
    request_hash,
    serialize_local_shopping_requests,
    sync_order_payment_status,
)
from ...services.outbox_service import process_email_outbox, process_whatsapp_outbox
from ...services.notification_service import NotificationPayload, NotificationService
from ...services.payment_refund_security import (
    create_payment_receipt,
    create_refund_request,
    issue_signed_receipt_url,
    list_payment_receipts_for_review,
    require_finance_actor,
    review_payment_receipt,
    signed_receipt_file_response,
    update_refund_workflow_status,
)
from ...services.payment_methods import (
    PAYMENT_METHODS_SETTING_KEY,
    normalize_payment_method_rows,
    payment_methods_payload,
    read_payment_method_rows,
)
from ...services.public_read_cache import cache_key, public_read_cache
from ...services.product_identifier import decode_compact_uuid
from ...services.realtime import (
    REALTIME_PROTOCOL,
    RealtimeEventService,
    RealtimeTicketService,
    extract_realtime_ticket,
    receive_secure_message,
    realtime_hub,
)
from ...services.report_admin_services import (
    AdminCustomerAccessService,
    BootstrapVisibilityService,
    CampaignService,
    CourierLocationService,
    FormSettingsPersistenceService,
    LoyaltyTierService,
    OperationalDayService,
    ReportGenerationService,
    RevenueRecognitionService,
    SupportWorkflowService,
    SyncCursorService,
    ThemeAdminService,
    _date_range,
)
from ...services.r2_migration import R2MigrationService
from ...services.secure_backup import BackupCoordinator
from ...storage import FileStorage, StoragePolicyRegistry
from .commerce import _delete_product_file_assets, _serialize_orders_with_financials


router = APIRouter(tags=["operations"])
storage = FileStorage()
SEED_UPLOADS_ZIP = BACKEND_DIR / "seed_data" / "uploads_seed.zip"
SEED_UPLOADS_SAMPLE = "products/0039c8877ec3f5759d10cb9b.webp"
ADMIN_NOTIFICATION_ROLES = {"admin", "manager"}
DIRECT_RECEIPT_INPUT_FIELDS = frozenset(
    {
        "receipt_url",
        "receiptUrl",
        "image_url",
        "imageUrl",
        "proof_url",
        "proofUrl",
        "base64",
        "dataBase64",
        "external_url",
        "externalUrl",
    }
)


async def _record_and_publish_realtime(
    session: AsyncSession,
    *,
    channel: str,
    event: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    recorded = await RealtimeEventService().record_event(
        session,
        channel=channel,
        event=event,
        payload=payload,
        dedupe_key=dedupe_key,
        user_id=user_id,
    )
    await realtime_hub.publish_recorded_event(
        channel,
        {
            "event": event,
            "type": event,
            "payload": payload,
            "event_id": recorded.get("event_id") or recorded.get("id"),
            "channel": channel,
        },
    )
    return recorded


PAYMENT_RECORD_STATUSES = frozenset({"pending", "confirmed", "approved", "paid", "rejected"})
UPLOAD_FORBIDDEN_CLIENT_FIELDS = frozenset(
    {
        "bucket",
        "path",
        "storage_path",
        "storagePath",
        "storage_key",
        "storageKey",
        "url",
        "fileUrl",
        "file_url",
        "dataBase64",
        "base64",
        "externalUrl",
        "external_url",
    }
)


def _file_asset_response(asset: FileAsset, *, public_url: str | None) -> dict[str, Any]:
    return {
        "id": str(asset.id),
        "file_id": str(asset.id),
        "url": public_url,
        "path": f"file:{asset.id}",
        "category": asset.policy_key,
        "policy_key": asset.policy_key,
        "mime_type": asset.content_type,
        "size_bytes": asset.size_bytes,
        "sha256": asset.checksum_sha256,
        "visibility": asset.visibility,
        "scan_status": asset.scan_status,
        "storage_ref": f"file:{asset.id}",
    }


def _asset_public_url(request: Request, asset: FileAsset) -> str | None:
    if asset.visibility != "public":
        return None
    if asset.storage_provider == "cloudflare_r2":
        base_url = str(get_settings().r2_public_base_url).rstrip("/")
        return f"{base_url}/{asset.storage_key.lstrip('/')}"
    return f"{str(request.base_url).rstrip('/')}/uploads/{asset.storage_key}"


def _roles_can_access_file(asset: FileAsset, user: User, roles: set[str]) -> bool:
    if roles.intersection({"admin", "manager", "staff", "finance"}):
        return True
    if asset.owner_user_id == user.id or asset.created_by == user.id:
        return True
    if "partner" in roles and asset.owner_user_id == user.id:
        return True
    return False


async def _record_file_asset(
    session: AsyncSession,
    *,
    stored,
    user: User,
    owner_user_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    image_metadata: dict[str, Any] | None = None,
) -> FileAsset:
    asset = FileAsset(
        owner_user_id=owner_user_id or user.id,
        created_by=user.id,
        policy_key=stored.policy_key,
        visibility=stored.visibility,
        storage_provider=stored.storage_provider,
        storage_bucket=stored.storage_bucket,
        storage_key=stored.relative_path,
        original_filename=stored.original_filename,
        content_type=stored.content_type,
        size_bytes=stored.size,
        checksum_sha256=stored.sha256,
        status="available",
        scan_status=stored.scan_status,
        scan_provider=stored.scan_provider,
        entity_type=entity_type,
        entity_id=entity_id,
        extra_data={
            "quarantine_path": stored.quarantine_path,
            "upload_policy": stored.policy_key,
            "storage_visibility": stored.visibility,
            **(image_metadata or {}),
        },
    )
    session.add(asset)
    audit_model = MODEL_BY_TABLE["audit_logs"]
    session.add(
        audit_model(
            user_id=user.id,
            type="file.uploaded",
            description=f"Uploaded {stored.policy_key} file",
            extra_data={
                "file_id": str(asset.id),
                "policy_key": stored.policy_key,
                "visibility": stored.visibility,
                "size_bytes": stored.size,
                "sha256": stored.sha256,
                **(image_metadata or {}),
            },
        )
    )
    await session.flush()
    await session.refresh(asset)
    return asset


async def _secure_upload_from_request(
    request: Request,
    *,
    user: User,
    roles: set[str],
    session: AsyncSession,
    forced_policy: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict) and UPLOAD_FORBIDDEN_CLIENT_FIELDS.intersection(body.keys()):
                raise HTTPException(status_code=422, detail="multipart_file_required")
        raise HTTPException(status_code=422, detail="multipart_file_required")
    form = await request.form()
    forbidden = UPLOAD_FORBIDDEN_CLIENT_FIELDS.intersection(form.keys())
    if forbidden:
        raise HTTPException(status_code=422, detail="client_storage_fields_forbidden")
    uploaded = form.get("file")
    if not isinstance(uploaded, UploadFile) and not (hasattr(uploaded, "filename") and hasattr(uploaded, "read")):
        raise HTTPException(status_code=422, detail="missing_file")
    policy_key = forced_policy or str(form.get("purpose") or form.get("category") or "").strip()
    policy = StoragePolicyRegistry.resolve(policy_key)
    if forced_policy is None and policy.key == "payment_receipt":
        raise HTTPException(status_code=422, detail="payment_receipts_must_use_order_endpoint")
    data = await uploaded.read()
    image_metadata: dict[str, Any] = {}
    upload_content_type = str(getattr(uploaded, "content_type", "") or "").lower()
    upload_filename = str(getattr(uploaded, "filename", "") or "file")
    if upload_content_type.startswith("image/") or Path(upload_filename).suffix.lower() in {
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"
    }:
        # Scan the original bytes before the image pipeline normalizes them.
        # Re-encoding can remove an appended active-content signature and
        # would otherwise let a malicious upload reach the storage scanner as
        # a clean transformed image.
        if policy.requires_scan:
            raw_scan = storage.scanner.scan(data, upload_content_type)
            if raw_scan.status != "clean":
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "malware_or_active_content_detected",
                        "scan_status": raw_scan.status,
                        "scan_provider": raw_scan.provider,
                        "signature": raw_scan.signature,
                    },
                )
        try:
            prepared = await prepare_image_upload(
                data,
                upload_filename,
                upload_content_type,
                policy_key=policy.key,
                max_bytes=min(policy.max_bytes, get_settings().max_upload_bytes),
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=415, detail="invalid_image_upload") from exc
        data = prepared.data
        file_name = prepared.filename
        image_metadata = {
            "image_pipeline": "webp",
            "image_width": prepared.width,
            "image_height": prepared.height,
            "original_size_bytes": prepared.original_size_bytes,
            "ai_enhanced": prepared.enhanced,
            "ai_provider": prepared.provider,
        }
    else:
        file_name = upload_filename
    stored = storage.save_bytes(policy.key, file_name, data, str(request.base_url), roles=roles)
    asset = await _record_file_asset(
        session,
        stored=stored,
        user=user,
        owner_user_id=owner_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        image_metadata=image_metadata,
    )
    if commit:
        await session.commit()
    return _file_asset_response(asset, public_url=_asset_public_url(request, asset))


def _uuid(value: Any, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"invalid_uuid:{field}")


async def _rows(session: AsyncSession, table: str, *, clauses=(), limit=500, order_desc=True, order_by=()):
    model = MODEL_BY_TABLE[table]
    statement = select(model)
    if "deleted_at" in model.__table__.c:
        statement = statement.where(model.__table__.c.deleted_at.is_(None))
    if clauses:
        statement = statement.where(*clauses)
    if order_by:
        statement = statement.order_by(*order_by)
    elif "created_at" in model.__table__.c:
        statement = statement.order_by(model.__table__.c.created_at.desc() if order_desc else model.__table__.c.created_at.asc())
    result = await session.execute(statement.limit(limit))
    return list(result.scalars())


PUBLIC_STATUS_VALUES = frozenset({"", "active", "approved", "published", "enabled", "visible"})


def _is_public_listing_row(row: dict[str, Any]) -> bool:
    if row.get("deleted_at"):
        return False
    if row.get("is_active") is False:
        return False
    status = str(row.get("status") or "").strip().lower()
    return status in PUBLIC_STATUS_VALUES


def _sort_public_listing_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (int(row.get("sort_order") or 0), str(row.get("name") or row.get("name_en") or "")))


def _money_from_payload(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value or "0"))
    except Exception:
        raise HTTPException(status_code=422, detail="invalid_amount")
    if amount < 0:
        raise HTTPException(status_code=422, detail="invalid_amount")
    return amount


def _first_text(*values: Any, default: str = "") -> str:
    for value in values:
        text_value = str(value or "").strip()
        if text_value:
            return text_value
    return default


async def _public_resource_rows(session: AsyncSession, table: str, *, limit: int = 500) -> list[dict[str, Any]]:
    rows = [serialize_record(row) for row in await _rows(session, table, limit=limit)]
    return _sort_public_listing_rows([row for row in rows if _is_public_listing_row(row)])


def _partner_option_from_storefront(row: dict[str, Any]) -> dict[str, Any]:
    partner_id = str(row.get("partner_id") or row.get("user_id") or row.get("id") or "")
    store_name = _first_text(row.get("store_name"), row.get("name"), row.get("name_en"), default="متجر محلي")
    return {
        **row,
        "id": str(row.get("id") or partner_id),
        "user_id": partner_id,
        "partner_id": partner_id,
        "store_name": store_name,
        "full_name": store_name,
    }


def _payment_account_options() -> list[dict[str, Any]]:
    allowed = {method.strip().lower() for method in get_settings().payment_method_allowlist}
    methods = [
        ("cash_on_delivery", "cash_on_delivery", "الدفع عند الاستلام", "cash"),
        ("wallet_transfer", "wallet_transfer", "تحويل محفظة", "wallet"),
        ("bank_transfer", "bank_transfer", "تحويل بنكي", "bank"),
        ("jaib", "JAIB", "محفظة جيب", "wallet"),
        ("jawali", "JAWALI", "جوالي", "wallet"),
        ("yemen_wallet", "YEMEN_WALLET", "يمن والت", "wallet"),
        ("one_cash", "ONE_CASH", "ون كاش", "wallet"),
        ("haseb_kuraimi", "HASEB_KURAIMI", "حاسب الكريمي", "bank"),
    ]
    accounts: list[dict[str, Any]] = []
    for account_id, method, label, account_type in methods:
        if method.lower() not in allowed and account_id.lower() not in allowed:
            continue
        accounts.append(
            {
                "id": account_id,
                "payment_method": method,
                "display_name": label,
                "account_name": label,
                "account_number": method,
                "type": account_type,
                "is_active": True,
            }
        )
    return accounts

def _jsonable(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


async def _count(session: AsyncSession, table: str, *clauses: Any) -> int:
    model = MODEL_BY_TABLE.get(table)
    if model is None:
        return 0
    statement = select(func.count()).select_from(model)
    if "deleted_at" in model.__table__.c:
        statement = statement.where(model.__table__.c.deleted_at.is_(None))
    if clauses:
        statement = statement.where(*clauses)
    return int((await session.execute(statement)).scalar_one())


async def _customer_count(session: AsyncSession, *clauses: Any) -> int:
    """Count active customer accounts without including staff/partner users."""
    statement = (
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .where(User.deleted_at.is_(None), UserRole.role == "customer")
    )
    if clauses:
        statement = statement.where(*clauses)
    return int((await session.execute(statement)).scalar_one())


async def _sum_amount(session: AsyncSession, table: str, *clauses: Any, column: str = "amount") -> Decimal:
    model = MODEL_BY_TABLE.get(table)
    if model is None or column not in model.__table__.c:
        return Decimal("0")
    statement = select(func.coalesce(func.sum(model.__table__.c[column]), 0)).select_from(model)
    if "deleted_at" in model.__table__.c:
        statement = statement.where(model.__table__.c.deleted_at.is_(None))
    if clauses:
        statement = statement.where(*clauses)
    return Decimal(str((await session.execute(statement)).scalar_one() or 0))


async def _status_counts(session: AsyncSession, table: str, column: str = "status") -> dict[str, int]:
    model = MODEL_BY_TABLE.get(table)
    if model is None or column not in model.__table__.c:
        return {}
    statement = select(model.__table__.c[column], func.count()).select_from(model).group_by(model.__table__.c[column])
    if "deleted_at" in model.__table__.c:
        statement = statement.where(model.__table__.c.deleted_at.is_(None))
    result = await session.execute(statement)
    return {str(status or "unknown"): int(count) for status, count in result.all()}


async def _public_rows(
    session: AsyncSession,
    table: str,
    *,
    clauses=(),
    limit: int = 500,
    order_desc: bool = False,
) -> list[Any]:
    model = MODEL_BY_TABLE[table]
    statement = select(model)
    columns = model.__table__.c
    if "deleted_at" in columns:
        statement = statement.where(columns.deleted_at.is_(None))
    if "is_active" in columns:
        statement = statement.where(columns.is_active.is_(True))
    if "status" in columns:
        statement = statement.where(columns.status.notin_(["disabled", "inactive", "deleted", "draft"]))
    if clauses:
        statement = statement.where(*clauses)
    if "sort_order" in columns:
        statement = statement.order_by(columns.sort_order)
    elif "created_at" in columns:
        statement = statement.order_by(columns.created_at.desc() if order_desc else columns.created_at.asc())
    result = await session.execute(statement.limit(limit))
    return list(result.scalars())


def _add_audit_log(session: AsyncSession, user_id: uuid.UUID, action: str, description: str) -> None:
    model = MODEL_BY_TABLE["audit_logs"]
    session.add(model(user_id=user_id, type=action, description=description))


def _storage_diagnostics() -> dict[str, Any]:
    settings = get_settings()
    upload_dir = settings.resolved_upload_dir
    products_dir = upload_dir / "products"
    product_file_count = 0
    if products_dir.is_dir():
        product_file_count = sum(1 for path in products_dir.rglob("*") if path.is_file())
    return {
        "upload_dir_exists": upload_dir.is_dir(),
        "upload_dir_name": upload_dir.name,
        "upload_dir_writable": upload_dir.is_dir() and os.access(upload_dir, os.W_OK),
        "product_upload_files": product_file_count,
        "seed_uploads_zip_exists": SEED_UPLOADS_ZIP.is_file(),
        "seed_uploads_zip_bytes": SEED_UPLOADS_ZIP.stat().st_size if SEED_UPLOADS_ZIP.is_file() else 0,
        "seed_sample_exists": (upload_dir / SEED_UPLOADS_SAMPLE).is_file(),
    }


@router.get("/health")
@router.get("/api/health")
async def health():
    settings = get_settings()
    public_payload = {
        "status": "ok",
        "mode": "postgresql",
        "database": "connected",
        "database_connected": True,
    }
    # Keep detailed counts and runtime diagnostics available to local
    # development/tests, but never expose database names, fixture flags, or
    # catalog counts from the public production health endpoint.
    if settings.app_env not in {"development", "test"}:
        return public_payload
    async with SessionFactory() as session:
        counts = {}
        for table in ("users", "products", "categories", "orders"):
            model = MODEL_BY_TABLE[table]
            counts[table] = int((await session.execute(select(func.count()).select_from(model))).scalar_one())
    return {
        **public_payload,
        "app_env": settings.app_env,
        "database_name": settings.database_name,
        "allow_test_fixtures": settings.allow_test_fixtures,
        "fixtures_enabled": settings.fixtures_enabled,
        "storage_environment": settings.storage_environment,
        "storage": _storage_diagnostics(),
        **counts,
    }


@router.get("/health/live")
async def health_live():
    settings = get_settings()
    if settings.app_env not in {"development", "test"}:
        return {"status": "ok"}
    return {
        "status": "ok",
        "app_env": settings.app_env,
        "database_name": settings.database_name,
        "fixtures_enabled": settings.fixtures_enabled,
        "storage_environment": settings.storage_environment,
        "storage": _storage_diagnostics(),
    }


@router.get("/health/storage")
async def health_storage(_: User = Depends(require_admin)):
    return storage.r2_diagnostics()


async def _critical_schema_ready(session: AsyncSession) -> dict[str, Any]:
    required_tables = (
        "users",
        "user_roles",
        "profiles",
        "refresh_tokens",
        "login_attempts",
        "account_security",
        "refresh_token_security",
        "verification_tokens",
        "password_reset_token_state",
        "phone_otp_tokens",
        "notification_preferences",
        "push_tokens",
        "web_push_subscriptions",
        "notification_outbox",
        "notification_delivery_attempts",
    )
    result = await session.execute(
        text(
            """
            select unnest(cast(:required_tables as text[])) as table_name
            except
            select tablename from pg_catalog.pg_tables where schemaname = 'public'
            """
        ),
        {"required_tables": list(required_tables)},
    )
    missing = [str(row[0]) for row in result]
    return {"ready": not missing, "missing_tables": missing}


@router.get("/health/ready")
async def health_ready(session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    critical_schema = {"ready": False, "missing_tables": []}
    try:
        # Reuse the request session for both probes.  Opening one engine
        # connection in database_ready() and another one for the schema query
        # added a full network round trip to every Render health check.
        await session.execute(text("SELECT 1"))
        critical_schema = await _critical_schema_ready(session)
    except Exception:
        critical_schema = {"ready": False, "missing_tables": ["schema_check_failed"]}
    ready = bool(critical_schema.get("ready"))
    public_payload = {
        "status": "ok" if ready else "unavailable",
        "mode": "postgresql",
        "database": "connected" if ready else "unavailable",
        "database_connected": ready,
    }
    if settings.app_env not in {"development", "test"}:
        return public_payload
    return {
        **public_payload,
        "critical_schema": critical_schema,
        "app_env": settings.app_env,
        "database_name": settings.database_name,
        "fixtures_enabled": settings.fixtures_enabled,
        "storage_environment": settings.storage_environment,
        "storage": _storage_diagnostics(),
    }


def _alembic_head_revision() -> str | None:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        script = ScriptDirectory.from_config(cfg)
        return script.get_current_head()
    except Exception:
        return None


@router.get("/version")
@router.get("/api/version")
async def api_version():
    settings = get_settings()
    commit_sha = (
        os.environ.get("RENDER_GIT_COMMIT")
        or os.environ.get("GIT_COMMIT")
        or os.environ.get("SOURCE_VERSION")
        or "unknown"
    )
    build_time = os.environ.get("BUILD_TIME") or os.environ.get("RENDER_BUILD_CREATED_AT")
    return {
        "commit_sha": commit_sha,
        "build_time": build_time,
        "app_env": settings.app_env,
        "alembic_revision": _alembic_head_revision(),
        "api_version": "2.0.0",
    }


@router.get("/deployment/status")
async def deployment_status(session: AsyncSession = Depends(get_session)):
    version = (await session.execute(select(func.max(MODEL_BY_TABLE["deployment_checks"].created_at)))).scalar_one_or_none()
    return {"status": "ready", "database": "postgresql", "state_json_runtime": False, "last_check": version.isoformat() if version else None}


@router.get("/sync/status")
async def sync_status(
    stream: str = Query("default"),
    device_id: str = Query("server", alias="deviceId"),
    platform: str = Query("unknown"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    return await SyncCursorService().status(session, user=user, stream=stream, device_id=device_id, platform=platform)


@router.post("/api/sync/{stream}/pull")
async def sync_pull(stream: str, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await SyncCursorService().pull(session, user=user, stream=stream, body=await request.json())


@router.get("/sync/bootstrap")
async def sync_bootstrap(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await BootstrapVisibilityService.bootstrap(session, user=user)


def _site_content_payload(row: Any) -> dict[str, Any]:
    """Expose site content using the contract consumed by the admin/storefront UI.

    ``site_content`` is a compatibility table whose canonical columns are
    ``name`` and ``body``.  The frontend contract uses ``key`` and
    ``content``; image URLs and metadata live in ``extra_data``.
    """
    extra = dict(getattr(row, "extra_data", None) or {})
    payload = serialize_record(row)
    payload.update(
        {
            "key": str(getattr(row, "name", None) or extra.get("key") or ""),
            "content": getattr(row, "body", None),
            "image_url": extra.get("image_url") or extra.get("imageUrl"),
            "metadata": extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {},
        }
    )
    return payload


@router.get("/api/content/site")
@router.get("/content/site")
async def public_site_content(session: AsyncSession = Depends(get_session)):
    return await public_read_cache.get_or_set(
        cache_key("content-site"),
        lambda: _public_site_content_uncached(session),
    )


async def _public_site_content_uncached(session: AsyncSession) -> dict[str, Any]:
    return {"data": [_site_content_payload(row) for row in await _public_rows(session, "site_content")]}


@router.get("/content/menus")
async def public_site_menus(admin: bool = False, session: AsyncSession = Depends(get_session)):
    return await public_read_cache.get_or_set(
        cache_key("content-menus", admin=admin),
        lambda: _public_site_menus_uncached(session),
    )


async def _public_site_menus_uncached(session: AsyncSession) -> dict[str, Any]:
    return {"data": [serialize_record(row) for row in await _public_rows(session, "site_menus")]}


@router.get("/content/social-links")
async def public_social_links(admin: bool = False, session: AsyncSession = Depends(get_session)):
    return await public_read_cache.get_or_set(
        cache_key("content-social-links", admin=admin),
        lambda: _public_social_links_uncached(session),
    )


async def _public_social_links_uncached(session: AsyncSession) -> dict[str, Any]:
    return {"data": [serialize_record(row) for row in await _public_rows(session, "social_links")]}


@router.get("/content/theme")
async def public_theme_settings(session: AsyncSession = Depends(get_session)):
    # Theme values are small and must reflect an admin publish immediately.
    # Do not serve a process-local snapshot here; the API middleware also
    # marks this response as no-store.
    return await _public_theme_settings_uncached(session)


async def _public_theme_settings_uncached(session: AsyncSession) -> dict[str, Any]:
    return {"data": [serialize_record(row) for row in await _public_rows(session, "theme_settings", limit=200)]}


@router.get("/content/settings/public/{setting_key}")
async def public_setting(setting_key: str, session: AsyncSession = Depends(get_session)):
    return await public_read_cache.get_or_set(
        cache_key("content-setting", setting_key=setting_key),
        lambda: _public_setting_uncached(setting_key, session),
    )


async def _public_setting_uncached(setting_key: str, session: AsyncSession) -> dict[str, Any]:
    model = MODEL_BY_TABLE["site_settings"]
    columns = model.__table__.c
    clauses = []
    if "name" in columns:
        clauses.append(columns.name == setting_key)
    if "setting_key" in columns:
        clauses.append(columns.setting_key == setting_key)
    if not clauses:
        return {"data": None}
    result = await session.execute(select(model).where(or_(*clauses)).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        return {"data": None}
    record = serialize_record(row)
    return {"data": {"setting_key": setting_key, "setting_value": record.get("setting_value") or record.get("value") or record.get("extra_data") or record}}


@router.get("/content/sections")
async def public_page_sections(page: str = "home", session: AsyncSession = Depends(get_session)):
    return await public_read_cache.get_or_set(
        cache_key("content-sections", page=page),
        lambda: _public_page_sections_uncached(page, session),
    )


async def _public_page_sections_uncached(page: str, session: AsyncSession) -> dict[str, Any]:
    model = MODEL_BY_TABLE["page_sections"]
    columns = model.__table__.c
    clauses = []
    if "page" in columns:
        clauses.append(columns.page == page)
    elif "page_key" in columns:
        clauses.append(columns.page_key == page)
    # The storefront needs to distinguish "no page configuration exists"
    # from "the administrator intentionally hid every section".  The old
    # generic public-row filter removed hidden sections before the client could
    # make that distinction, so visibility/order controls appeared ineffective.
    statement = select(model).where(*clauses)
    if "deleted_at" in columns:
        statement = statement.where(columns.deleted_at.is_(None))
    if "sort_order" in columns:
        statement = statement.order_by(columns.sort_order)
    result = await session.execute(statement.limit(500))
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.get("/content/pages/{slug}")
async def public_static_page(slug: str, session: AsyncSession = Depends(get_session)):
    return await public_read_cache.get_or_set(
        cache_key("content-page", slug=slug),
        lambda: _public_static_page_uncached(slug, session),
    )


async def _public_static_page_uncached(slug: str, session: AsyncSession) -> dict[str, Any]:
    model = MODEL_BY_TABLE["static_pages"]
    columns = model.__table__.c
    clauses = [columns.slug == slug]
    if "is_active" in columns:
        clauses.append(columns.is_active.is_(True))
    if "status" in columns:
        clauses.append(columns.status.in_(("active", "published")))
    result = await session.execute(select(model).where(*clauses).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="page_not_found")
    return {"data": serialize_record(row)}


@router.get("/suppliers")
async def public_suppliers(
    type: str | None = Query(None),
    active: bool | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    return await public_read_cache.get_or_set(
        cache_key("public-suppliers", type=type, active=active),
        lambda: _public_suppliers_uncached(type, active, session),
    )


async def _public_suppliers_uncached(type: str | None, active: bool | None, session: AsyncSession) -> dict[str, Any]:
    model = MODEL_BY_TABLE["suppliers"]
    columns = model.__table__.c
    clauses = []
    if type and "supplier_type" in columns:
        clauses.append(or_(columns.supplier_type == type, columns.supplier_type == "both"))
    if active is not None and "is_active" in columns:
        clauses.append(columns.is_active.is_(active))
    rows = await _public_rows(session, "suppliers", clauses=clauses, limit=500)
    return {"data": [serialize_record(row) for row in rows]}


@router.get("/suppliers/counts/products")
async def supplier_product_counts(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Product.supplier_id, Product.partner_id, func.count(Product.id))
        .where(*public_product_clauses(Product))
        .group_by(Product.supplier_id, Product.partner_id)
    )
    counts: dict[str, int] = {"main-store": 0}
    for supplier_id, partner_id, count in result.all():
        safe_count = int(count or 0)
        if supplier_id:
            counts[str(supplier_id)] = counts.get(str(supplier_id), 0) + safe_count
        if partner_id:
            counts[str(partner_id)] = counts.get(str(partner_id), 0) + safe_count
        if supplier_id is None and partner_id is None:
            counts["main-store"] += safe_count
    return {"data": counts}


@router.get("/suppliers/counts/orders")
async def supplier_order_counts(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(OrderItem.partner_id, func.count(OrderItem.id))
        .where(OrderItem.partner_id.is_not(None))
        .group_by(OrderItem.partner_id)
    )
    return {"data": {str(partner_id): int(count) for partner_id, count in result.all() if partner_id}}


@router.get("/partner/storefront")
async def get_partner_storefront(user: User = Depends(require_partner), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["partner_storefronts"]
    result = await session.execute(select(model).where(or_(model.user_id == user.id, model.partner_id == user.id)).limit(1))
    row = result.scalar_one_or_none()
    # Keep the read response aligned with the write response and the web API
    # client.  Previously PUT returned {data: ...}, while GET returned the
    # record directly.  That made a successful save look lost after reload in
    # clients that correctly read response.data.
    return {"data": serialize_record(row) if row else {}}


@router.put("/partner/storefront")
async def save_partner_storefront(request: Request, user: User = Depends(require_partner), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE["partner_storefronts"]
    result = await session.execute(select(model).where(or_(model.user_id == user.id, model.partner_id == user.id)).with_for_update())
    row = result.scalar_one_or_none()
    if row is None:
        row = model(
            user_id=user.id,
            partner_id=user.id,
            name=str(body.get("storeName") or body.get("name") or "متجر"),
            status="pending",
            is_active=False,
        )
        session.add(row)
    mapping = {"storeName": "name", "name": "name", "email": "email", "phone": "phone", "description": "description", "storeDescription": "description", "logoUrl": "logo_url", "storeLogoUrl": "logo_url"}
    for source, target in mapping.items():
        if source in body:
            setattr(row, target, body[source])
    # Flush and refresh before serializing so the response is the same value a
    # new request will read after the transaction has committed.  This also
    # makes database defaults and server-side timestamps visible immediately.
    await session.flush()
    await session.commit()
    await session.refresh(row)
    return {"data": serialize_record(row)}


@router.get("/admin/partner-storefronts")
async def admin_partner_storefronts(
    limit: int = Query(500, ge=1, le=2000),
    status: str | None = Query(None),
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["partner_storefronts"]
    clauses = [model.deleted_at.is_(None)]
    normalized_status = str(status or "").strip().lower()
    if normalized_status:
        clauses.append(model.status == normalized_status)
    result = await session.execute(
        select(model)
        .where(*clauses)
        .order_by(model.updated_at.desc(), model.created_at.desc())
        .limit(limit)
    )
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.post("/admin/partner-storefronts/{storefront_id}/review")
async def review_partner_storefront(
    storefront_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    status = str(body.get("status") or "").lower().strip()
    if status not in {"approved", "rejected"}:
        raise HTTPException(status_code=422, detail="invalid_storefront_status")
    reason = str(
        body.get("reason")
        or body.get("rejectionReason")
        or body.get("approvalNotes")
        or ""
    ).strip()
    if status == "rejected" and not reason:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "storefront_rejection_reason_required",
                "message": "اكتب سبب رفض المتجر قبل الحفظ.",
            },
        )
    model = MODEL_BY_TABLE["partner_storefronts"]
    row = await session.get(model, storefront_id, with_for_update=True)
    if row is None or getattr(row, "deleted_at", None) is not None:
        raise HTTPException(status_code=404, detail="storefront_not_found")
    row.status = status
    row.is_active = status == "approved"
    if "extra_data" in row.__table__.c:
        metadata = dict(getattr(row, "extra_data", None) or {})
        metadata["review_reason"] = reason if status == "rejected" else None
        metadata["reviewed_by"] = str(admin.id)
        metadata["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        row.extra_data = metadata
    partner_id = getattr(row, "partner_id", None) or getattr(row, "user_id", None)
    if partner_id:
        title = (
            "مبروك تمت الموافقة على متجرك"
            if status == "approved"
            else "تم رفض طلب متجرك"
        )
        message = (
            "تمت الموافقة على متجرك وأصبح جاهزاً لاستقبال المنتجات والعملاء."
            if status == "approved"
            else f"سبب الرفض: {reason}"
        )
        await NotificationService(session).create_notification(
            NotificationPayload(
                user_id=partner_id,
                title=title,
                body=message,
                notification_type=f"storefront_{status}",
                category="partner",
                priority="high",
                entity_type="partner_storefronts",
                entity_id=str(row.id),
                created_by=admin.id,
                deduplication_key=f"storefront-review:{row.id}:{status}:{getattr(row, 'updated_at', None)}",
            )
        )
    await session.commit()
    return {"data": serialize_record(row)}


def _partner_notification_preferences_payload(row: Any | None) -> dict[str, Any]:
    defaults = {
        "partner_id": None,
        "orders_enabled": True,
        "promotions_enabled": False,
        "updates_enabled": True,
        "weekly_enabled": True,
        "status": "active",
        "is_active": True,
    }
    if row is None:
        return defaults
    payload = serialize_record(row)
    # Older production schemas keep the channel switches in ``extra_data``
    # instead of dedicated boolean columns.  Merge those values before
    # applying defaults so a successful save remains visible after reload.
    extra_data = getattr(row, "extra_data", None)
    if isinstance(extra_data, dict):
        for key in defaults:
            if key in extra_data and extra_data[key] is not None:
                payload[key] = extra_data[key]
    payload.update({
        key: payload.get(key, fallback)
        for key, fallback in defaults.items()
        if key not in payload or payload.get(key) is None
    })
    return payload


@router.get("/partner/notification-preferences")
@router.get("/api/partner/notification-preferences")
async def get_partner_notification_preferences(
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["partner_notification_preferences"]
    row = (
        await session.execute(
            select(model)
            .where(model.partner_id == user.id, model.deleted_at.is_(None))
            .order_by(model.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {"data": _partner_notification_preferences_payload(row)}


@router.put("/partner/notification-preferences")
@router.patch("/partner/notification-preferences")
@router.put("/api/partner/notification-preferences")
@router.patch("/api/partner/notification-preferences")
async def save_partner_notification_preferences(
    request: Request,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    model = MODEL_BY_TABLE["partner_notification_preferences"]
    row = (
        await session.execute(
            select(model)
            .where(model.partner_id == user.id, model.deleted_at.is_(None))
            .order_by(model.updated_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = model(partner_id=user.id, status="active", is_active=True)
        session.add(row)
    aliases = {
        "orders_enabled": ("orders_enabled", "ordersEnabled", "order_updates"),
        "promotions_enabled": ("promotions_enabled", "promotionsEnabled", "promotional_notifications"),
        "updates_enabled": ("updates_enabled", "updatesEnabled", "system_notifications"),
        "weekly_enabled": ("weekly_enabled", "weeklyEnabled"),
    }
    for target, keys in aliases.items():
        for key in keys:
            if key in body:
                value = body[key]
                if not isinstance(value, bool):
                    raise HTTPException(status_code=422, detail=f"{target}_must_be_boolean")
                # The production table is intentionally extensible and keeps
                # channel-specific switches in extra_data.  Older deployments
                # do not have dedicated boolean columns, so assigning an
                # unmapped SQLAlchemy attribute would appear to succeed but
                # would be lost after the request committed.
                if target in row.__table__.c:
                    setattr(row, target, value)
                else:
                    extra_data = dict(getattr(row, "extra_data", {}) or {})
                    extra_data[target] = value
                    row.extra_data = extra_data
                break
    row.status = "active"
    row.is_active = True
    await session.flush()
    await session.commit()
    await session.refresh(row)
    return {"data": _partner_notification_preferences_payload(row)}


@router.get("/partner/agreement")
@router.get("/api/partner/agreement")
async def get_partner_agreement(
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["partner_contracts"]
    row = (
        await session.execute(
            select(model)
            .where(model.partner_id == user.id, model.deleted_at.is_(None))
            .order_by(model.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    payload = serialize_record(row) if row is not None else {}
    extra = row.extra_data if row is not None and isinstance(row.extra_data, dict) else {}
    payload.update(extra)
    payload.setdefault("version", "1.0")
    payload.setdefault("status", "pending")
    payload["accepted"] = payload.get("accepted") is True or payload.get("status") in {"accepted", "active"} and payload.get("accepted_at") is not None
    return {"data": payload}


@router.post("/partner/agreement/accept")
@router.post("/api/partner/agreement/accept")
async def accept_partner_agreement(
    request: Request,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    if body.get("confirmed") is not True:
        raise HTTPException(status_code=422, detail="agreement_confirmation_required")
    model = MODEL_BY_TABLE["partner_contracts"]
    row = (
        await session.execute(
            select(model)
            .where(model.partner_id == user.id, model.deleted_at.is_(None))
            .order_by(model.updated_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc).isoformat()
    if row is None:
        row = model(partner_id=user.id, status="active", is_active=True, extra_data={})
        session.add(row)
    row.status = "active"
    row.is_active = True
    row.extra_data = {
        **(row.extra_data or {}),
        "version": str(body.get("version") or "1.0"),
        "accepted": True,
        "accepted_at": now,
        "accepted_by": str(user.id),
        "acceptance_source": "merchant_app",
    }
    await session.commit()
    payload = serialize_record(row)
    payload.update(row.extra_data or {})
    payload["accepted"] = True
    return {"data": payload}


_PARTNER_OPTION_TABLES = {
    "brands": "brands",
    "colors": "color_options",
    "sizes": "size_options",
}


def _partner_option_table(option: str) -> str:
    table = _PARTNER_OPTION_TABLES.get(option.strip().lower())
    if table is None:
        raise HTTPException(status_code=404, detail="partner_option_not_found")
    return table


def _partner_option_clause(model: Any, partner_id: uuid.UUID):
    return model.extra_data["partner_id"].astext == str(partner_id)


@router.get("/partner/product-options/{option}")
async def list_partner_product_options(
    option: str,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    table = _partner_option_table(option)
    rows = await _rows(session, table, limit=500)
    # Global catalog options are available to every merchant. A merchant may
    # also see options it owns, but never options created by another merchant.
    visible_rows = []
    for row in rows:
        if getattr(row, "is_active", True) is False:
            continue
        extra_data = getattr(row, "extra_data", None) or {}
        owner_id = extra_data.get("partner_id") if isinstance(extra_data, dict) else None
        if owner_id in (None, "", str(user.id)):
            visible_rows.append(row)
    return {"data": [serialize_record(row) for row in visible_rows]}


@router.post("/partner/product-options/{option}", status_code=201)
async def create_partner_product_option(
    option: str,
    request: Request,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    table = _partner_option_table(option)
    body = await request.json()
    name = _first_text(body.get("name"), body.get("label"))
    if not name:
        raise HTTPException(status_code=422, detail="option_name_required")
    row = await _api_create(
        session,
        table,
        {"name": name, "code": body.get("code"), "is_active": True, "status": "active", "partner_id": user.id},
        user,
    )
    await session.commit()
    return {"data": row}


@router.patch("/partner/product-options/{option}/{record_id}")
async def update_partner_product_option(
    option: str,
    record_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    table = _partner_option_table(option)
    model = MODEL_BY_TABLE[table]
    row = await session.get(model, record_id, with_for_update=True)
    if row is None or (row.extra_data or {}).get("partner_id") != str(user.id):
        raise HTTPException(status_code=404, detail="partner_option_not_found")
    body = await request.json()
    row.name = _first_text(body.get("name"), body.get("label"), row.name)
    if hasattr(row, "code") and body.get("code") is not None:
        row.code = str(body.get("code") or "")
    await session.commit()
    return {"data": serialize_record(row)}


@router.delete("/partner/product-options/{option}/{record_id}")
async def delete_partner_product_option(
    option: str,
    record_id: uuid.UUID,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    table = _partner_option_table(option)
    model = MODEL_BY_TABLE[table]
    row = await session.get(model, record_id, with_for_update=True)
    if row is None or (row.extra_data or {}).get("partner_id") != str(user.id):
        raise HTTPException(status_code=404, detail="partner_option_not_found")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.get("/partner/coupons")
async def list_partner_coupons(
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    rows = await _rows(
        session,
        "partner_coupons",
        clauses=(MODEL_BY_TABLE["partner_coupons"].partner_id == user.id,),
        limit=500,
    )
    return {"data": [serialize_record(row) for row in rows]}


@router.post("/partner/coupons", status_code=201)
async def create_partner_coupon(
    request: Request,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    code = _first_text(body.get("code")).upper()
    if not code:
        raise HTTPException(status_code=422, detail="coupon_code_required")
    row = await _api_create(
        session,
        "partner_coupons",
        {
            "partner_id": user.id,
            "code": code,
            "amount": _money_from_payload(body.get("discount_value") or body.get("amount") or 0),
            "status": "active",
            "is_active": True,
        },
        user,
    )
    await session.commit()
    return {"data": row}


@router.patch("/partner/coupons/{record_id}")
async def update_partner_coupon(
    record_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["partner_coupons"]
    row = await session.get(model, record_id, with_for_update=True)
    if row is None or row.partner_id != user.id:
        raise HTTPException(status_code=404, detail="coupon_not_found")
    body = await request.json()
    if body.get("code") is not None:
        row.code = _first_text(body.get("code"), row.code).upper()
    if body.get("discount_value") is not None or body.get("amount") is not None:
        row.amount = _money_from_payload(body.get("discount_value") or body.get("amount"))
    await session.commit()
    return {"data": serialize_record(row)}


@router.delete("/partner/coupons/{record_id}")
async def delete_partner_coupon(
    record_id: uuid.UUID,
    user: User = Depends(require_partner),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["partner_coupons"]
    row = await session.get(model, record_id, with_for_update=True)
    if row is None or row.partner_id != user.id:
        raise HTTPException(status_code=404, detail="coupon_not_found")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.get("/admin/partner-applications")
async def partner_applications(limit: int = 500, admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return [serialize_record(row) for row in await _rows(session, "partner_applications", limit=limit)]


@router.post("/admin/partner-applications/{application_id}/review")
async def review_partner_application(application_id: uuid.UUID, request: Request, admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    status = str(body.get("status") or "").lower()
    if status not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="invalid_application_status")
    reason = str(body.get("reason") or body.get("rejectionReason") or "").strip()
    if status == "rejected" and not reason:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "partner_rejection_reason_required",
                "message": "اكتب سبب رفض طلب المتجر قبل الحفظ.",
            },
        )
    model = MODEL_BY_TABLE["partner_applications"]
    result = await session.execute(select(model).where(model.id == application_id).with_for_update())
    application = result.scalar_one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail="application_not_found")
    application.status = status
    application.reviewed_by = admin.id
    application.reviewed_at = datetime.now(timezone.utc)
    if "extra_data" in application.__table__.c:
        metadata = dict(getattr(application, "extra_data", None) or {})
        metadata["review_reason"] = reason if status == "rejected" else None
        application.extra_data = metadata
    if status == "approved" and application.user_id:
        approved_user = await session.get(User, application.user_id, with_for_update=True)
        if approved_user is not None:
            account_state = await account_security_for(session, approved_user.id, for_update=True)
            approved_user.is_active = True
            account_state.account_status = "active"
            account_state.disabled_at = None
            if account_state.email_verified_at is None:
                account_state.email_verified_at = application.reviewed_at
            await bump_security_version(session, approved_user, reason="merchant_approved", request=request)
        role = await session.get(UserRole, {"user_id": application.user_id, "role": "partner"})
        if role is None:
            session.add(UserRole(user_id=application.user_id, role="partner"))
        storefront_model = MODEL_BY_TABLE["partner_storefronts"]
        storefront_result = await session.execute(select(storefront_model).where(storefront_model.partner_id == application.user_id))
        storefront = storefront_result.scalar_one_or_none()
        if storefront is None:
            storefront = storefront_model(
                user_id=application.user_id, partner_id=application.user_id,
                name=application.name or "متجر", email=application.email, phone=application.phone,
                status="active", description=application.description, logo_url=application.logo_url, is_active=True,
            )
            session.add(storefront)
        else:
            storefront.status = "active"
            storefront.is_active = True
    elif status == "rejected" and application.user_id:
        rejected_user = await session.get(User, application.user_id, with_for_update=True)
        if rejected_user is not None:
            account_state = await account_security_for(session, rejected_user.id, for_update=True)
            current_roles = set(await roles_for(session, rejected_user.id))
            protected_roles = {"admin", "manager", "finance", "logistics", "staff", "employee", "courier", "delivery"}
            if not current_roles.intersection(protected_roles):
                rejected_user.is_active = False
                account_state.account_status = "merchant_rejected"
                account_state.disabled_at = application.reviewed_at
                await revoke_all_refresh_tokens(session, rejected_user.id, now=application.reviewed_at)
            if "partner" in current_roles:
                role = await session.get(UserRole, {"user_id": rejected_user.id, "role": "partner"})
                if role is not None:
                    await session.delete(role)
            await bump_security_version(session, rejected_user, reason="merchant_rejected", request=request)
    if application.user_id:
        title = "تمت الموافقة على طلب متجرك" if status == "approved" else "تم رفض طلب المتجر"
        message = (
            "تمت الموافقة على طلب متجرك ويمكنك الآن تجهيز المنتجات للمراجعة."
            if status == "approved"
            else f"سبب الرفض: {reason}"
        )
        await NotificationService(session).create_notification(
            NotificationPayload(
                user_id=application.user_id,
                title=title,
                body=message,
                notification_type=f"partner_application_{status}",
                category="partner",
                priority="high",
                entity_type="partner_applications",
                entity_id=str(application.id),
                created_by=admin.id,
                deduplication_key=f"partner-application-review:{application.id}:{status}",
            )
        )
    await session.commit()
    approved_user = await session.get(User, application.user_id) if application.user_id else None
    return {"application": serialize_record(application), "auth": await auth_payload(session, approved_user, issue_tokens=False) if approved_user else None}


@router.get("/admin/customers")
async def admin_customers(
    limit: int = 500,
    admin: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await AdminCustomerAccessService.list_customers(session, roles=roles, limit=limit, full=True)


@router.get("/api/admin/account-deletion-requests")
async def admin_account_deletion_requests(
    status: str = Query("pending"),
    limit: int = Query(200, ge=1, le=1000),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    request_model = MODEL_BY_TABLE["account_deletion_requests"]
    profile_model = MODEL_BY_TABLE["profiles"]
    normalized_status = str(status or "pending").strip().lower()
    statement = (
        select(request_model, User, profile_model)
        .join(User, User.id == request_model.user_id)
        .outerjoin(
            profile_model,
            and_(
                profile_model.user_id == User.id,
                profile_model.deleted_at.is_(None),
            ),
        )
        .where(request_model.deleted_at.is_(None))
        .order_by(request_model.created_at.desc())
        .limit(limit)
    )
    if normalized_status != "all":
        statement = statement.where(request_model.status == normalized_status)
    result = await session.execute(statement)
    data = []
    for deletion_request, user, profile in result.all():
        data.append(
            {
                **serialize_record(deletion_request),
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "is_active": bool(user.is_active),
                    "full_name": getattr(profile, "full_name", None) if profile else None,
                    "phone": getattr(profile, "phone", None) if profile else None,
                },
            }
        )
    return {"data": data}


@router.patch("/api/admin/account-deletion-requests/{request_id}")
async def review_account_deletion_request(
    request_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    next_status = str(body.get("status") or "").strip().lower()
    if next_status not in {"approved", "rejected"}:
        raise HTTPException(status_code=422, detail="invalid_account_deletion_status")

    request_model = MODEL_BY_TABLE["account_deletion_requests"]
    deletion_request = await session.get(request_model, request_id, with_for_update=True)
    if deletion_request is None or getattr(deletion_request, "deleted_at", None) is not None:
        raise HTTPException(status_code=404, detail="account_deletion_request_not_found")
    if str(deletion_request.status or "").lower() not in {"pending", "processing"}:
        raise HTTPException(status_code=409, detail="account_deletion_request_already_reviewed")

    target_user = await session.get(User, deletion_request.user_id, with_for_update=True)
    if target_user is None:
        raise HTTPException(status_code=404, detail="account_owner_not_found")

    now = datetime.now(timezone.utc)
    deletion_request.status = next_status
    if "extra_data" in deletion_request.__table__.c:
        metadata = dict(getattr(deletion_request, "extra_data", None) or {})
        metadata["reviewed_by"] = str(admin.id)
        metadata["reviewed_at"] = now.isoformat()
        metadata["review_reason"] = str(body.get("reason") or "").strip() or None
        deletion_request.extra_data = metadata

    account_state = await account_security_for(session, target_user.id, for_update=True)
    if next_status == "approved":
        target_user.is_active = False
        target_user.deleted_at = now
        account_state.account_status = "deleted"
        account_state.disabled_at = now
        await bump_security_version(session, target_user, reason="account_deletion_approved")
        await revoke_all_refresh_tokens(session, target_user.id, now=now)
    else:
        target_user.is_active = True
        target_user.deleted_at = None
        account_state.account_status = "active"
        account_state.disabled_at = None
        await bump_security_version(session, target_user, reason="account_deletion_rejected")

    await session.commit()
    return {"data": serialize_record(deletion_request), "status": next_status}


SECTION_TABLES = {
    "suppliers": "suppliers", "warehouses": "warehouses", "inventory": "inventory",
    "brands": "brands", "categories": "categories", "merchants": "partner_profiles",
    "currencies": "currencies", "banners": "banners", "forms": "form_settings",
}


CONTENT_TABLES = {
    "menus": "site_menus",
    "social-links": "social_links",
    "theme": "theme_settings",
    "site": "site_settings",
    # Custom elements have their own persistence table.  Sending them to
    # page_sections made the dashboard appear to save successfully while the
    # element could not be read back by the storefront.
    "custom-elements": "custom_elements",
    "sections": "page_sections",
    "pages": "static_pages",
    "blog": "site_content",
    "forms": "form_settings",
    "shipping-zones": "shipping_zones",
}


PUBLIC_CONTENT_STATUSES = {"", "active", "published", "enabled", "visible", "live"}


def _blog_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "off", "inactive", "disabled", "draft", "unpublished"}:
            return False
        if normalized in {"true", "1", "yes", "on", "active", "enabled", "published", "visible", "live"}:
            return True
    return bool(value)


def _blog_article_is_published(row: dict[str, Any]) -> bool:
    if "is_published" in row:
        return _blog_bool(row.get("is_published"), default=False)
    if row.get("is_active") is False:
        return False
    return str(row.get("status") or "").strip().lower() in PUBLIC_CONTENT_STATUSES


def _blog_article_payload(value: Any) -> dict[str, Any]:
    item = serialize_record(value) if hasattr(value, "__table__") else dict(value or {})
    extra = dict(getattr(value, "extra_data", None) or {}) if hasattr(value, "__table__") else dict(item.get("extra_data") or {})

    def first_text(*keys: str, default: str = "") -> str:
        for key in keys:
            candidate = item.get(key)
            if candidate is None:
                candidate = extra.get(key)
            if str(candidate or "").strip():
                return str(candidate).strip()
        return default

    published = _blog_article_is_published({**extra, **item})
    published_at = item.get("published_at") or extra.get("published_at")
    if published and not published_at:
        published_at = item.get("created_at")
    keywords = item.get("keywords")
    if keywords is None:
        keywords = extra.get("keywords")
    if isinstance(keywords, str):
        keywords = [word.strip() for word in keywords.split(",") if word.strip()]
    if not isinstance(keywords, list):
        keywords = []
    try:
        views_count = int(item.get("views_count") or extra.get("views_count") or 0)
    except (TypeError, ValueError):
        views_count = 0
    return {
        **item,
        "title": first_text("title", "name", default="مقال بدون عنوان"),
        "title_en": item.get("title_en") or extra.get("title_en"),
        "slug": first_text("slug", default=str(item.get("id") or "")),
        "excerpt": item.get("excerpt") if item.get("excerpt") is not None else extra.get("excerpt"),
        "excerpt_en": item.get("excerpt_en") if item.get("excerpt_en") is not None else extra.get("excerpt_en"),
        "content": item.get("content") or item.get("body") or extra.get("content") or "",
        "content_en": item.get("content_en") or extra.get("content_en"),
        "cover_image": item.get("cover_image") or item.get("image_url") or extra.get("cover_image"),
        "meta_title": item.get("meta_title") or extra.get("meta_title"),
        "meta_description": item.get("meta_description") or extra.get("meta_description"),
        "keywords": keywords,
        "category": first_text("category", default="general"),
        "author_name": first_text("author_name", default="رفاهية التسوق"),
        "is_published": published,
        "is_featured": _blog_bool(item.get("is_featured") if item.get("is_featured") is not None else extra.get("is_featured")),
        "views_count": views_count,
        "published_at": published_at,
        "created_by": item.get("created_by") or extra.get("created_by"),
    }


async def _blog_source_rows(session: AsyncSession) -> list[Any]:
    """Read the canonical blog table and the legacy site-content table.

    Older deployments wrote articles to ``site_content`` while the resource
    schema exposes ``blog_articles``. Reading both keeps existing content
    visible and lets new writes use one durable table.
    """
    rows: list[Any] = []
    seen_keys: set[str] = set()
    for table in ("blog_articles", "site_content"):
        for row in await _rows(session, table, limit=500):
            payload = _blog_article_payload(row)
            key = str(payload.get("slug") or payload.get("id") or "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            rows.append(row)
    return rows


def _normalize_blog_body(
    body: Any,
    *,
    existing: Any = None,
    for_create: bool = False,
    table: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="blog_payload_required")
    table = table or (str(existing.__table__.name) if existing is not None else "blog_articles")
    model = MODEL_BY_TABLE[table]
    values: dict[str, Any] = {}
    extra = dict(getattr(existing, "extra_data", None) or {}) if existing is not None else {}
    for key, value in body.items():
        if key in model.__table__.c and key not in {"id", "created_at", "updated_at", "deleted_at", "extra_data"}:
            values[key] = _jsonable(value)
        elif key not in {"content", "cover_image", "is_published", "published_at"}:
            extra[key] = _jsonable(value)

    title = _first_text(body.get("title"), body.get("name"))
    if for_create or "title" in body or "name" in body:
        if len(title) < 2:
            raise HTTPException(status_code=422, detail="blog_title_required")
        values["title"] = title
        if "name" in model.__table__.c:
            values["name"] = title
    slug = _first_text(body.get("slug"), body.get("name"), title)
    if slug and "slug" in model.__table__.c:
        values["slug"] = "-".join(slug.split())[:500]
    content = body.get("content") if "content" in body else body.get("body")
    if content is not None:
        values["body"] = str(content)
        extra["content"] = str(content)
    elif for_create:
        raise HTTPException(status_code=422, detail="blog_content_required")
    if "cover_image" in body:
        extra["cover_image"] = _jsonable(body.get("cover_image"))
        if "image_url" in model.__table__.c:
            values["image_url"] = _jsonable(body.get("cover_image"))

    current_item = _blog_article_payload(existing) if existing is not None else {}
    if "is_published" in body:
        published = _blog_bool(body.get("is_published"))
    elif for_create:
        published = False
    elif "status" in body:
        published = str(body.get("status") or "").strip().lower() in PUBLIC_CONTENT_STATUSES
    else:
        published = _blog_bool(current_item.get("is_published"), default=False)
    values["status"] = "published" if published else "draft"
    if "is_active" in model.__table__.c:
        values["is_active"] = published
    extra["is_published"] = published
    if "published_at" in body:
        extra["published_at"] = _jsonable(body.get("published_at"))
    elif published:
        extra.setdefault("published_at", current_item.get("published_at") or datetime.now(timezone.utc).isoformat())
    else:
        extra["published_at"] = None
    return values, extra


async def public_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    try:
        return await optional_user(request=request, credentials=credentials, session=session)
    except HTTPException:
        return None


async def _resource_data(session: AsyncSession, table: str, *, limit: int = 500) -> list[dict[str, Any]]:
    if table not in RESOURCE_TABLES:
        raise HTTPException(status_code=404, detail="resource_not_found")
    return [serialize_record(row) for row in await _rows(session, table, limit=limit)]


def _public_content_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public_rows: list[dict[str, Any]] = []
    for row in rows:
        if row.get("deleted_at"):
            continue
        if row.get("is_active") is False:
            continue
        status = str(row.get("status") or "").strip().lower()
        if status not in PUBLIC_CONTENT_STATUSES:
            continue
        if "is_published" in row and not _blog_bool(row.get("is_published"), default=False):
            continue
        public_rows.append(row)
    return public_rows


def _public_setting_payload(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    match = next((row for row in rows if row.get("name") == key or row.get("key") == key or row.get("setting_key") == key), None)
    if match is None:
        return {"data": None}
    value = match.get("value")
    if value is None:
        value = match.get("extra_data")
    if value is None:
        metadata = {"id", "created_at", "updated_at", "deleted_at", "name", "key", "setting_key", "setting_value", "status", "is_active"}
        value = {field: field_value for field, field_value in match.items() if field not in metadata}
    return {"data": {**match, "setting_key": key, "setting_value": value}}


@router.get("/api/catalog/currencies")
async def api_catalog_currencies(session: AsyncSession = Depends(get_session)):
    return {"data": await _resource_data(session, "currencies")}


@router.get("/api/catalog/categories")
async def api_catalog_categories(session: AsyncSession = Depends(get_session)):
    category_model = MODEL_BY_TABLE["categories"]
    product_model = MODEL_BY_TABLE["products"]
    result = await session.execute(
        select(category_model, func.count(product_model.id).label("product_count"))
        .outerjoin(
            product_model,
            and_(product_model.category_id == category_model.id, *public_product_clauses(product_model)),
        )
        .where(category_model.deleted_at.is_(None), category_model.is_active.is_(True))
        .group_by(category_model.id)
        .order_by(category_model.sort_order.asc(), category_model.name.asc())
    )
    data = []
    for category, product_count in result.all():
        row = serialize_record(category)
        row["direct_product_count"] = int(product_count or 0)
        row["product_count"] = int(product_count or 0)
        data.append(row)
    return {"data": data}


@router.get("/api/catalog/brands")
async def api_catalog_brands(session: AsyncSession = Depends(get_session)):
    brand_model = MODEL_BY_TABLE["brands"]
    product_model = MODEL_BY_TABLE["products"]
    result = await session.execute(
        select(brand_model, func.count(product_model.id).label("product_count"))
        .outerjoin(
            product_model,
            and_(product_model.brand_id == brand_model.id, *public_product_clauses(product_model)),
        )
        .where(brand_model.deleted_at.is_(None), brand_model.is_active.is_(True))
        .group_by(brand_model.id)
        .order_by(brand_model.name.asc())
    )
    data = []
    for brand, product_count in result.all():
        row = serialize_record(brand)
        row["product_count"] = int(product_count or 0)
        data.append(row)
    return {"data": data}


@router.get("/api/catalog/products")
async def api_catalog_products(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=5000),
    limit: int | None = Query(None, ge=1, le=5000),
    q: str | None = None,
    categoryId: str | None = None,
    categoryIds: str | None = None,
    categorySlug: str | None = None,
    categoryName: str | None = None,
    brand: str | None = None,
    brandId: str | None = None,
    brandSlug: str | None = None,
    supplierId: str | None = None,
    mainStoreOnly: bool | None = None,
    minPrice: Decimal | None = None,
    maxPrice: Decimal | None = None,
    featured: bool | None = None,
    new_only: bool | None = None,
    newOnly: bool | None = None,
    is_new: bool | None = None,
    onSale: bool | None = None,
    sort: str = "newest",
    includeTotal: bool = Query(
        True,
        description="Return the exact COUNT(*) total. Mobile catalog views can disable this extra query.",
    ),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["products"]
    columns = model.__table__.c
    requested_page_size = page_size or limit
    offset = (page - 1) * requested_page_size if requested_page_size else 0
    clauses: list[Any] = public_product_clauses(model)

    if q:
        search = f"%{q.strip()}%"
        search_clauses = []
        for column_name in ("name", "name_en", "description", "sku", "short_code"):
            if column_name in columns:
                search_clauses.append(columns[column_name].ilike(search))
        if search_clauses:
            clauses.append(or_(*search_clauses))

    category_values = [value.strip() for value in (categoryIds or "").split(",") if value.strip()]
    if categoryId:
        category_values.append(categoryId)
    parsed_category_ids = []
    for value in category_values:
        try:
            parsed_category_ids.append(uuid.UUID(value))
        except ValueError:
            continue
    if parsed_category_ids and "category_id" in columns:
        clauses.append(columns["category_id"].in_(parsed_category_ids))

    category_model = MODEL_BY_TABLE.get("categories")
    if category_model is not None and "category_id" in columns and (categorySlug or categoryName):
        category_clauses = []
        if categorySlug and "slug" in category_model.__table__.c:
            category_clauses.append(category_model.__table__.c.slug == categorySlug)
        if categoryName:
            name_match = f"%{categoryName.strip()}%"
            for column_name in ("name", "name_en"):
                if column_name in category_model.__table__.c:
                    category_clauses.append(category_model.__table__.c[column_name].ilike(name_match))
        if category_clauses:
            category_ids = select(category_model.__table__.c.id).where(or_(*category_clauses))
            clauses.append(columns["category_id"].in_(category_ids))

    brand_values = [brandId] if brandId else []
    parsed_brand_ids = []
    for value in brand_values:
        try:
            parsed_brand_ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    if parsed_brand_ids and "brand_id" in columns:
        clauses.append(columns["brand_id"].in_(parsed_brand_ids))

    brand_model = MODEL_BY_TABLE.get("brands")
    if brand_model is not None and "brand_id" in columns and (brandSlug or brand):
        brand_clauses = []
        if brandSlug and "slug" in brand_model.__table__.c:
            brand_clauses.append(brand_model.__table__.c.slug == brandSlug)
        if brand:
            brand_match = f"%{brand.strip()}%"
            for column_name in ("name", "name_en"):
                if column_name in brand_model.__table__.c:
                    brand_clauses.append(brand_model.__table__.c[column_name].ilike(brand_match))
        if brand_clauses:
            brand_ids = select(brand_model.__table__.c.id).where(or_(*brand_clauses))
            clauses.append(columns["brand_id"].in_(brand_ids))

    if supplierId:
        for column_name in ("supplier_id", "partner_id"):
            if column_name in columns:
                try:
                    clauses.append(columns[column_name] == uuid.UUID(supplierId))
                except ValueError:
                    clauses.append(columns[column_name] == supplierId)
                break

    if mainStoreOnly is True and "partner_id" in columns:
        clauses.append(columns["partner_id"].is_(None))
    if featured is True and "is_featured" in columns:
        clauses.append(columns["is_featured"].is_(True))
    if new_only is True or newOnly is True or is_new is True:
        clauses.append(new_product_clause(model))
    if minPrice is not None and "price" in columns:
        clauses.append(columns["price"] >= minPrice)
    if maxPrice is not None and "price" in columns:
        clauses.append(columns["price"] <= maxPrice)
    if onSale is True and "original_price" in columns and "price" in columns:
        clauses.append(and_(columns["original_price"].is_not(None), columns["original_price"] > columns["price"]))

    total: int | None = None
    if includeTotal:
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(*clauses)
                )
            ).scalar_one()
        )
    statement = select(model).where(*clauses)
    if sort == "price_asc" and "price" in columns:
        statement = statement.order_by(columns["price"].asc())
    elif sort == "price_desc" and "price" in columns:
        statement = statement.order_by(columns["price"].desc())
    elif sort in {"name", "name_asc"} and "name" in columns:
        statement = statement.order_by(columns["name"].asc())
    elif "created_at" in columns:
        statement = statement.order_by(columns["created_at"].desc())
    if requested_page_size:
        statement = statement.offset(offset).limit(requested_page_size)
    result = await session.execute(statement)
    products = list(result.scalars())
    rows = await build_public_product_rows(session, products)
    effective_page_size = requested_page_size or total or len(products)
    total_pages = (
        (total + effective_page_size - 1) // effective_page_size
        if total and effective_page_size
        else 0
    )
    return {
        "items": rows,
        "data": rows,
        "total": total,
        "page": page,
        "page_size": effective_page_size,
        "limit": effective_page_size,
        "total_pages": total_pages,
        "has_next": bool(
            requested_page_size
            and (
                page < total_pages
                if includeTotal
                else len(products) >= requested_page_size
            )
        ),
        "has_previous": bool(
            requested_page_size
            and page > 1
            and (total_pages > 0 if includeTotal else True)
        ),
        "visibility": {
            "customer": "approved_catalog_excluding_private_statuses",
            "admin": "all_products_via_api_catalog_admin_products",
        },
    }


@router.get("/api/catalog/products/{identifier}")
async def api_catalog_product(identifier: str, session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["products"]
    clauses = []
    try:
        clauses.append(model.id == uuid.UUID(identifier))
    except ValueError:
        compact_uuid = decode_compact_uuid(identifier)
        if compact_uuid is not None:
            clauses.append(model.id == compact_uuid)
    if "short_code" in model.__table__.c:
        clauses.append(model.short_code == identifier)
    if "slug" in model.__table__.c:
        clauses.append(model.slug == identifier)
    if "sku" in model.__table__.c:
        clauses.append(model.sku == identifier)
    if not clauses:
        raise HTTPException(status_code=404, detail="product_not_found")
    row = (
        await session.execute(
            select(model).where(or_(*clauses), *public_product_clauses(model)).limit(1)
        )
    ).scalar_one_or_none()
    validate_public_product_or_404(row)
    rows = await build_public_product_rows(session, [row], include_variants=True)
    if not rows:
        raise HTTPException(status_code=404, detail="product_not_found")
    return {"data": rows[0]}


@router.get("/api/catalog/products/{product_id}/variants")
async def api_catalog_product_variants(product_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    product = (
        await session.execute(
            select(Product).where(Product.id == product_id, *public_product_clauses(Product)).limit(1)
        )
    ).scalar_one_or_none()
    validate_public_product_or_404(product)
    variants = (
        await session.execute(
            select(ProductVariant)
            .where(
                ProductVariant.product_id == product_id,
                ProductVariant.deleted_at.is_(None),
                ProductVariant.is_active.is_(True),
            )
            .order_by(ProductVariant.created_at.asc())
        )
    ).scalars()
    return {"data": [serialize_record(row) for row in variants]}


@router.get("/api/catalog/recommendations")
async def api_catalog_recommendations(
    currentProductId: uuid.UUID | None = None,
    categoryId: uuid.UUID | None = None,
    brandId: uuid.UUID | None = None,
    limit: int = Query(default=8, ge=1, le=24),
    session: AsyncSession = Depends(get_session),
):
    clauses: list[Any] = list(public_product_clauses(Product))
    if currentProductId:
        clauses.append(Product.id != currentProductId)
    if categoryId:
        clauses.append(Product.category_id == categoryId)
    if brandId:
        clauses.append(Product.brand_id == brandId)
    statement = select(Product).where(*clauses).order_by(Product.created_at.desc()).limit(limit)
    products = list((await session.execute(statement)).scalars())
    if not products and (categoryId or brandId):
        fallback_clauses: list[Any] = list(public_product_clauses(Product))
        if currentProductId:
            fallback_clauses.append(Product.id != currentProductId)
        products = list(
            (
                await session.execute(
                    select(Product).where(*fallback_clauses).order_by(Product.created_at.desc()).limit(limit)
                )
            ).scalars()
        )
    return {"data": await build_public_product_rows(session, products)}


@router.get("/api/catalog/settings")
async def api_catalog_settings(session: AsyncSession = Depends(get_session)):
    rows = await _resource_data(session, "site_settings")
    return {"data": {str(row.get("name") or row.get("key") or row.get("id")): row.get("extra_data") or row for row in rows}}


async def _create_resource_row(session: AsyncSession, table: str, body: dict[str, Any]) -> dict[str, Any]:
    if table == "categories":
        return await create_category_record(session, body)
    model = MODEL_BY_TABLE[table]
    values = {key: value for key, value in body.items() if key in model.__table__.c and key not in {"id", "created_at", "updated_at", "deleted_at", "extra_data"}}
    extra = {key: _jsonable(value) for key, value in body.items() if key not in model.__table__.c}
    row = model(**values, extra_data=extra) if "extra_data" in model.__table__.c else model(**values)
    session.add(row)
    await session.flush()
    return serialize_record(row)


@router.get("/api/admin/stats")
async def api_admin_stats(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    counts = {}
    for key in ("users", "products", "orders", "categories", "brands"):
        model = MODEL_BY_TABLE.get(key)
        if model is not None:
            counts[key] = int((await session.execute(select(func.count()).select_from(model))).scalar_one())
    return {"data": counts}


@router.get("/api/dashboard/live-kpis")
async def api_dashboard_live_kpis(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    week = today - timedelta(days=7)
    month = today.replace(day=1)
    order_model = MODEL_BY_TABLE["orders"]
    user_model = MODEL_BY_TABLE["users"]
    today_revenue = Decimal((await RevenueRecognitionService.summary(session, start=today))["net_revenue"])
    yesterday_revenue = Decimal((await RevenueRecognitionService.summary(session, start=yesterday, end=today))["net_revenue"])
    week_revenue = Decimal((await RevenueRecognitionService.summary(session, start=week))["net_revenue"])
    month_revenue = Decimal((await RevenueRecognitionService.summary(session, start=month))["net_revenue"])
    today_orders = await _count(session, "orders", order_model.created_at >= today)
    week_orders = await _count(session, "orders", order_model.created_at >= week)
    month_orders = await _count(session, "orders", order_model.created_at >= month)
    active_products = await _count(session, "products", MODEL_BY_TABLE["products"].is_active.is_(True))
    new_customers = await _customer_count(session, user_model.created_at >= week)
    month_order_count = max(month_orders, 1)
    target = Decimal("50000000")
    return {"data": {
        "todayRevenue": float(today_revenue),
        "yesterdayRevenue": float(yesterday_revenue),
        "weekRevenue": float(week_revenue),
        "monthRevenue": float(month_revenue),
        "todayOrders": today_orders,
        "weekOrders": week_orders,
        "monthOrders": month_orders,
        "avgOrderValue": float(month_revenue / month_order_count),
        "newCustomers": new_customers,
        "activeProducts": active_products,
        "targetProgress": float(min((month_revenue / target) * 100, Decimal("100"))) if target else 0,
        "pendingLocalRequests": await _count(session, "local_shopping_requests", MODEL_BY_TABLE["local_shopping_requests"].status.in_(("new", "pending", "reviewing"))),
        "pendingIntOrders": await _count(session, "international_orders", MODEL_BY_TABLE["international_orders"].status.in_(("new", "pending", "reviewing"))),
    }}


@router.get("/api/dashboard/operations-overview")
async def api_dashboard_operations_overview(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    order_statuses = await _status_counts(session, "orders")
    payment_statuses = await _status_counts(session, "orders", "payment_status")
    int_statuses = await _status_counts(session, "international_orders")
    local_statuses = await _status_counts(session, "local_shopping_requests")
    total_orders = sum(order_statuses.values())
    delivered = order_statuses.get("delivered", 0)
    paid = payment_statuses.get("paid", 0)
    return {"data": {
        "orderStatuses": order_statuses,
        "paymentStatuses": payment_statuses,
        "intStatuses": int_statuses,
        "localStatuses": local_statuses,
        "totalOrders": total_orders,
        "fulfillmentRate": round((delivered / total_orders) * 100, 2) if total_orders else 0,
        "collectionRate": round((paid / total_orders) * 100, 2) if total_orders else 0,
    }}


@router.get("/api/dashboard/system-health")
async def api_dashboard_system_health(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    order_model = MODEL_BY_TABLE["orders"]
    product_model = MODEL_BY_TABLE["products"]
    payment_receipt_model = MODEL_BY_TABLE["payment_receipts"]
    partner_model = MODEL_BY_TABLE["partner_applications"]
    partner_storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    marketer_model = MODEL_BY_TABLE["marketers"]
    security_model = MODEL_BY_TABLE["security_events"]
    stuck_cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    inventory_counts = _canonical_inventory_stock_counts(await _canonical_inventory_payloads(session))
    return {"data": {
        "total_orders": await _count(session, "orders"),
        "stuck_orders": await _count(session, "orders", order_model.status.in_(("pending", "new", "processing")), order_model.created_at < stuck_cutoff),
        "pending_payments": await _count(session, "payment_receipts", payment_receipt_model.status.in_(("pending", "uploaded", "reviewing"))),
        "balance_mismatch": bool(await _count(session, "orders", order_model.status == "delivered", order_model.payment_status != "paid")),
        "active_partners": await _count(session, "partner_storefronts", partner_storefront_model.status.in_(("active", "approved"))),
        "pending_partners": await _count(session, "partner_applications", partner_model.status.in_(("pending", "reviewing"))),
        "unsettled": float(await _sum_amount(session, "partner_settlements", MODEL_BY_TABLE["partner_settlements"].status.in_(("pending", "unpaid")))),
        "active_marketers": await _count(session, "marketers", marketer_model.status.in_(("active", "approved"))),
        "pending_commissions": float(await _sum_amount(session, "marketer_commissions", MODEL_BY_TABLE["marketer_commissions"].status.in_(("pending", "unpaid")))),
        "pending_international": await _count(session, "international_orders", MODEL_BY_TABLE["international_orders"].status.in_(("pending", "new", "reviewing"))),
        # Do not infer a missing link from workflow status. A processed
        # international order can still be unlinked, while a pending one can
        # already be linked. Use the same bidirectional relation resolver as
        # the order-linking screen so dashboard alerts and the screen agree.
        "unlinked": await _unlinked_international_count(session),
        "out_of_stock": inventory_counts["out_of_stock"],
        "low_stock": inventory_counts["low_stock"],
        "blocked_actions": await _count(session, "security_events", security_model.status.in_(("blocked", "rejected"))),
    }}


@router.get("/api/dashboard/quick-stats")
async def api_dashboard_quick_stats(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    order_model = MODEL_BY_TABLE["orders"]
    receipt_model = MODEL_BY_TABLE["payment_receipts"]
    product_model = MODEL_BY_TABLE["products"]
    partner_model = MODEL_BY_TABLE["partner_applications"]
    ticket_model = MODEL_BY_TABLE["support_tickets"]
    return {"data": {
        "pendingOrders": await _count(session, "orders", order_model.status.in_(("pending", "new", "processing"))),
        "pendingPayments": await _count(session, "payment_receipts", receipt_model.status.in_(("pending", "uploaded", "reviewing"))),
        "pendingProducts": await _count(session, "products", product_model.approval_status.in_(("pending", "reviewing"))),
        "pendingPartners": await _count(session, "partner_applications", partner_model.status.in_(("pending", "reviewing"))),
        "pendingIntOrders": await _count(session, "international_orders", MODEL_BY_TABLE["international_orders"].status.in_(("pending", "new", "reviewing"))),
        "pendingLocalRequests": await _count(session, "local_shopping_requests", MODEL_BY_TABLE["local_shopping_requests"].status.in_(("pending", "new", "reviewing"))),
        "unreadMessages": await _count(session, "support_tickets", ticket_model.status.in_(("new", "open", "pending"))),
        "totalPendingCommissions": float(await _sum_amount(session, "marketer_commissions", MODEL_BY_TABLE["marketer_commissions"].status.in_(("pending", "unpaid")))),
        "totalPendingSettlements": float(await _sum_amount(session, "partner_settlements", MODEL_BY_TABLE["partner_settlements"].status.in_(("pending", "unpaid")))),
    }}


@router.get("/api/dashboard/pending-actions")
async def api_dashboard_pending_actions(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    items: list[dict[str, Any]] = []
    order_model = MODEL_BY_TABLE["orders"]
    for row in await _rows(session, "orders", clauses=(order_model.status.in_(("pending", "new", "processing")),), limit=8):
        items.append({"id": str(row.id), "type": "order", "title": f"Order {row.order_number}", "subtitle": row.status, "time": row.created_at.isoformat(), "priority": "high" if row.created_at < datetime.now(timezone.utc) - timedelta(days=1) else "normal", "link": f"/admin/orders?highlight={row.id}"})
    partner_model = MODEL_BY_TABLE["partner_applications"]
    for row in await _rows(session, "partner_applications", clauses=(partner_model.status.in_(("pending", "reviewing")),), limit=5):
        items.append({"id": str(row.id), "type": "partner", "title": row.name or row.email or "Partner application", "subtitle": row.status, "time": row.created_at.isoformat(), "priority": "normal", "link": "/admin/partner-applications"})
    return {"data": items[:12]}


@router.get("/api/dashboard/smart-alerts")
async def api_dashboard_smart_alerts(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    order_model = MODEL_BY_TABLE["orders"]
    product_model = MODEL_BY_TABLE["products"]
    stuck_orders = []
    for row in await _rows(session, "orders", clauses=(order_model.status.in_(("pending", "new", "processing")), order_model.created_at < datetime.now(timezone.utc) - timedelta(days=1)), limit=10):
        stuck_orders.append({"id": str(row.id), "order_number": row.order_number, "created_at": row.created_at.isoformat()})
    inventory_counts = _canonical_inventory_stock_counts(await _canonical_inventory_payloads(session))
    return {"data": {
        "pending_payments": await _count(session, "payment_receipts", MODEL_BY_TABLE["payment_receipts"].status.in_(("pending", "uploaded", "reviewing"))),
        "low_stock": inventory_counts["low_stock"],
        "out_of_stock": inventory_counts["out_of_stock"],
        "pending_partners": await _count(session, "partner_applications", MODEL_BY_TABLE["partner_applications"].status.in_(("pending", "reviewing"))),
        "balance_issues": await _count(session, "orders", order_model.status == "delivered", order_model.payment_status != "paid"),
        "pending_commissions": float(await _sum_amount(session, "marketer_commissions", MODEL_BY_TABLE["marketer_commissions"].status.in_(("pending", "unpaid")))),
        "pending_settlements": float(await _sum_amount(session, "partner_settlements", MODEL_BY_TABLE["partner_settlements"].status.in_(("pending", "unpaid")))),
        "unlinked_international": await _unlinked_international_count(session),
        "stuck_orders": stuck_orders,
    }}


@router.get("/api/dashboard/sales-chart")
async def api_dashboard_sales_chart(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    orders_rows = [serialize_record(row) for row in await _rows(session, "orders", limit=200)]
    return {"data": {"orders": orders_rows, "categorySales": []}}


@router.get("/api/dashboard/recent-activity")
async def api_dashboard_recent_activity(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    rows = []
    for row in await _rows(session, "audit_logs", limit=20):
        rows.append({"id": str(row.id), "type": getattr(row, "type", "activity"), "title": getattr(row, "type", "activity"), "description": getattr(row, "description", ""), "time": row.created_at.isoformat()})
    return {"data": rows}


@router.get("/api/dashboard/financial-reconciliation")
async def api_dashboard_financial_reconciliation(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": {
        "ordersTotal": float(await _sum_amount(session, "orders", column="total")),
        "paymentsTotal": float(await _sum_amount(session, "order_payments")),
        "pendingReceipts": await _count(session, "payment_receipts", MODEL_BY_TABLE["payment_receipts"].status.in_(("pending", "uploaded", "reviewing"))),
        "mismatches": [],
    }}


@router.get("/api/dashboard/inventory-alerts")
async def api_dashboard_inventory_alerts(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": {"inventory": await _canonical_inventory_payloads(session), "movements": [serialize_record(row) for row in await _rows(session, "inventory_movements", limit=200)]}}


@router.get("/api/dashboard/customer-insights")
async def api_dashboard_customer_insights(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "orders", limit=200)]}


@router.get("/api/dashboard/sales-insights")
async def api_dashboard_sales_insights(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "orders", limit=200)]}


@router.get("/api/dashboard/marketer-stats")
async def api_dashboard_marketer_stats(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": {"marketers": [serialize_record(row) for row in await _rows(session, "marketers", limit=200)], "commissions": [serialize_record(row) for row in await _rows(session, "marketer_commissions", limit=200)]}}


@router.get("/api/admin/partners/options")
async def api_admin_partner_options(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    profile_model = MODEL_BY_TABLE["profiles"]
    result = await session.execute(
        select(storefront_model)
        .where(storefront_model.deleted_at.is_(None), storefront_model.is_active.is_(True))
        .order_by(storefront_model.name.asc(), storefront_model.created_at.desc())
        .limit(1000)
    )
    storefronts = list(result.scalars())
    partner_ids = {
        value
        for storefront in storefronts
        for value in (getattr(storefront, "partner_id", None), getattr(storefront, "user_id", None))
        if value
    }
    profiles: dict[uuid.UUID, Profile] = {}
    if partner_ids:
        profile_result = await session.execute(
            select(profile_model).where(profile_model.user_id.in_(partner_ids))
        )
        profiles = {profile.user_id: profile for profile in profile_result.scalars()}
    data = []
    for storefront in storefronts:
        partner_id = getattr(storefront, "partner_id", None) or getattr(storefront, "user_id", None)
        if partner_id is None:
            continue
        profile = profiles.get(partner_id)
        storefront_row = serialize_record(storefront)
        profile_name = getattr(profile, "full_name", None) if profile else None
        store_name = _first_text(
            storefront_row.get("name"),
            getattr(profile, "store_name", None) if profile else None,
            profile_name,
            storefront_row.get("email"),
            default="متجر التاجر",
        )
        data.append({
            **storefront_row,
            "id": str(partner_id),
            "user_id": str(partner_id),
            "partner_id": str(partner_id),
            "full_name": profile_name or store_name,
            "store_name": store_name,
            "email": storefront_row.get("email") or getattr(profile, "email", None),
            "phone": storefront_row.get("phone") or getattr(profile, "phone", None),
        })
    return {"data": data}


@router.get("/api/partnership/users/options")
async def api_partnership_user_options(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    partner_role_ids = select(UserRole.user_id).where(UserRole.role == "partner")
    storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    storefront_partner_ids = select(storefront_model.partner_id).where(
        storefront_model.deleted_at.is_(None), storefront_model.partner_id.is_not(None)
    )
    storefront_user_ids = select(storefront_model.user_id).where(
        storefront_model.deleted_at.is_(None), storefront_model.user_id.is_not(None)
    )
    result = await session.execute(
        select(User, Profile)
        .outerjoin(Profile, Profile.user_id == User.id)
        .where(
            User.deleted_at.is_(None),
            User.is_active.is_(True),
            User.id.not_in(partner_role_ids),
            User.id.not_in(storefront_partner_ids),
            User.id.not_in(storefront_user_ids),
        )
        .order_by(Profile.full_name.asc().nullslast(), User.email.asc())
        .limit(1000)
    )
    data = []
    for user, profile in result.all():
        data.append({
            "id": str(user.id),
            "user_id": str(user.id),
            "email": user.email,
            "full_name": getattr(profile, "full_name", None) or user.email,
            "phone": getattr(profile, "phone", None),
            "city": getattr(profile, "city", None),
        })
    return {"data": data}


@router.get("/api/admin/customers")
async def api_admin_customers_alias(
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await AdminCustomerAccessService.list_customers(session, roles=roles, limit=500, full=bool(roles.intersection({"admin", "manager"})))}


@router.patch("/api/admin/customers/{user_id}/status")
async def api_admin_update_customer_status(
    user_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if not roles.intersection({"admin", "manager"}):
        raise HTTPException(status_code=403, detail="customer_status_permission_denied")
    target = await session.get(User, user_id, with_for_update=True)
    if target is None or target.deleted_at is not None:
        raise HTTPException(status_code=404, detail="customer_not_found")
    if target.id == staff.id:
        raise HTTPException(status_code=409, detail="cannot_change_own_status")
    target_roles = set(await roles_for(session, target.id))
    if target_roles.intersection({"admin", "manager"}):
        raise HTTPException(status_code=403, detail="protected_staff_account")

    body = await request.json()
    raw_status = body.get("is_active", body.get("isActive"))
    if isinstance(raw_status, bool):
        is_active = raw_status
    elif isinstance(raw_status, (int, float)) and raw_status in {0, 1}:
        is_active = bool(raw_status)
    elif isinstance(raw_status, str) and raw_status.strip().lower() in {"true", "1", "yes", "on", "active", "enabled"}:
        is_active = True
    elif isinstance(raw_status, str) and raw_status.strip().lower() in {"false", "0", "no", "off", "inactive", "disabled"}:
        is_active = False
    else:
        raise HTTPException(status_code=422, detail="is_active_boolean_required")

    account_state = await account_security_for(session, target.id, for_update=True)
    target.is_active = is_active
    now = datetime.now(timezone.utc)
    if is_active:
        account_state.account_status = "active"
        account_state.disabled_at = None
        reason = "admin_customer_enabled"
    else:
        account_state.account_status = "admin_disabled"
        account_state.disabled_at = now
        reason = "admin_customer_disabled"
        await revoke_all_refresh_tokens(session, target.id, now=now)
    await bump_security_version(session, target, reason=reason, request=request)
    await session.commit()
    return {"data": {"id": str(target.id), "user_id": str(target.id), "email": target.email, "is_active": target.is_active}}


@router.patch("/api/admin/customers/{profile_id}")
async def api_admin_update_customer(
    profile_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if not roles.intersection({"admin", "manager"}):
        raise HTTPException(status_code=403, detail="customer_update_permission_denied")
    model = MODEL_BY_TABLE["profiles"]
    row = await session.get(model, profile_id)
    if row is None:
        row = (
            await session.execute(
                select(model).where(model.user_id == profile_id)
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="profile_not_found")
    body = await request.json()
    extra = dict(getattr(row, "extra_data", {}) or {})
    for key, value in body.items():
        if key in model.__table__.c and key not in {"id", "created_at", "user_id"}:
            setattr(row, key, value)
        else:
            extra[key] = _jsonable(value)
    if "extra_data" in model.__table__.c:
        row.extra_data = extra
    await session.commit()
    return {"data": serialize_record(row)}


@router.post("/api/admin/profiles/lookup")
async def api_admin_profiles_lookup(
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if not roles.intersection({"admin", "manager", "finance"}):
        raise HTTPException(status_code=403, detail="profile_lookup_permission_denied")
    body = await request.json()
    ids = [uuid.UUID(str(value)) for value in body.get("user_ids", [])]
    model = MODEL_BY_TABLE["profiles"]
    rows = (await session.execute(select(model).where(model.user_id.in_(ids)))).scalars().all() if ids else []
    data = []
    for row in rows:
        item = serialize_record(row)
        data.append({"id": item.get("id"), "user_id": item.get("user_id"), "full_name": item.get("full_name"), "phone": item.get("phone"), "city": item.get("city")})
    return {"data": data}


@router.get("/api/admin/contact-messages")
async def api_admin_contact_messages(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    data = []
    for row in await _rows(session, "contact_messages", limit=500):
        payload = serialize_record(row)
        # The contact_messages table stores the lifecycle in status. Expose
        # the UI-friendly read flag as well so replies/read actions survive a
        # reload without relying on a non-existent database column.
        payload["is_read"] = str(getattr(row, "status", "new") or "new").lower() != "new"
        data.append(payload)
    return {"data": data}


@router.get("/api/partnership/partners")
async def api_partnership_partners(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    profile_model = MODEL_BY_TABLE["profiles"]
    contract_model = MODEL_BY_TABLE["partner_contracts"]
    product_model = MODEL_BY_TABLE["products"]
    storefront_result = await session.execute(
        select(storefront_model)
        .where(storefront_model.deleted_at.is_(None))
        .order_by(storefront_model.created_at.desc())
        .limit(1000)
    )
    storefronts = list(storefront_result.scalars())
    partner_ids = {
        value
        for storefront in storefronts
        for value in (getattr(storefront, "partner_id", None), getattr(storefront, "user_id", None))
        if value
    }
    if not partner_ids:
        return {"data": []}
    profile_result = await session.execute(
        select(profile_model).where(profile_model.user_id.in_(partner_ids))
    )
    profiles = {profile.user_id: profile for profile in profile_result.scalars()}
    count_result = await session.execute(
        select(product_model.partner_id, func.count(product_model.id))
        .where(product_model.partner_id.in_(partner_ids), product_model.deleted_at.is_(None))
        .group_by(product_model.partner_id)
    )
    product_counts = {partner_id: int(count or 0) for partner_id, count in count_result.all()}
    contract_result = await session.execute(
        select(contract_model)
        .where(contract_model.partner_id.in_(partner_ids), contract_model.deleted_at.is_(None))
        .order_by(contract_model.updated_at.desc())
    )
    contracts = {}
    for contract in contract_result.scalars():
        contracts.setdefault(contract.partner_id, contract)
    data = []
    for storefront in storefronts:
        partner_id = getattr(storefront, "partner_id", None) or getattr(storefront, "user_id", None)
        if partner_id is None:
            continue
        row = serialize_record(storefront)
        profile = profiles.get(partner_id)
        contract = contracts.get(partner_id)
        contract_extra = getattr(contract, "extra_data", {}) if contract else {}
        if not isinstance(contract_extra, dict):
            contract_extra = {}
        name = _first_text(
            getattr(profile, "full_name", None) if profile else None,
            row.get("name"),
            row.get("email"),
            default="متجر التاجر",
        )
        rate = contract_extra.get("commissionRate", contract_extra.get("rate", 85))
        data.append({
            **row,
            "id": str(partner_id),
            "user_id": str(partner_id),
            "partner_id": str(partner_id),
            "name": name,
            "full_name": name,
            "store_name": row.get("name") or getattr(profile, "store_name", None) or name,
            "email": row.get("email") or getattr(profile, "email", None),
            "phone": row.get("phone") or getattr(profile, "phone", None) or "-",
            "city": getattr(profile, "city", None) or "-",
            "joinedAt": row.get("created_at"),
            "productsCount": product_counts.get(partner_id, 0),
            "commissionRate": rate,
            "status": row.get("status") or ("active" if row.get("is_active") else "inactive"),
        })
    return {"data": data}


@router.get("/api/partnership/applications")
async def api_partnership_applications(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "partner_applications", limit=500)]}


@router.get("/api/payments/accounts")
async def api_public_payment_accounts():
    return {"data": _payment_account_options()}


@router.get("/api/payment-methods")
async def api_public_payment_methods(session: AsyncSession = Depends(get_session)):
    return payment_methods_payload(await read_payment_method_rows(session))


@router.get("/api/admin/payment-methods")
async def api_admin_payment_methods(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return payment_methods_payload(await read_payment_method_rows(session))


@router.patch("/api/admin/payment-methods")
async def api_update_payment_methods(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    current = await read_payment_method_rows(session)
    rows = normalize_payment_method_rows(body.get("methods"), base_rows=current)
    model = MODEL_BY_TABLE["site_settings"]
    result = await session.execute(
        select(model)
        .where(model.name == PAYMENT_METHODS_SETTING_KEY, model.deleted_at.is_(None))
        .limit(1)
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = model(name=PAYMENT_METHODS_SETTING_KEY, status="active", is_active=True)
        session.add(setting)
    setting.status = "active"
    setting.is_active = True
    setting.extra_data = {"configured_by_admin": True, "methods": rows}
    _add_audit_log(
        session,
        admin.id,
        "admin.payment_methods.update",
        "Updated payment method availability",
    )
    await session.commit()
    return payment_methods_payload(rows)


@router.get("/api/shopping/global-sites")
async def api_public_global_sites(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await _public_resource_rows(session, "global_sites", limit=limit)}


@router.get("/api/shopping/local/options")
async def api_public_local_options(
    session: AsyncSession = Depends(get_session),
):
    merchants = await _public_resource_rows(session, "local_merchants", limit=500)
    storefronts = await _public_resource_rows(session, "partner_storefronts", limit=500)
    partners = [_partner_option_from_storefront(row) for row in storefronts]
    return {"data": {"merchants": merchants, "partners": partners}}


@router.get("/api/shopping/local/partners/{partner_id}/products")
async def api_public_local_partner_products(
    partner_id: str,
    session: AsyncSession = Depends(get_session),
):
    partner_uuid = _uuid(partner_id, "partner_id")
    product_model = MODEL_BY_TABLE["products"]
    result = await session.execute(
        select(product_model)
        .where(*public_product_clauses(product_model), product_model.partner_id == partner_uuid)
        .order_by(product_model.created_at.desc())
        .limit(500)
    )
    rows = await build_public_product_rows(session, list(result.scalars()), include_variants=True)
    return {"data": rows}


@router.get("/api/shopping/local/requests")
async def api_user_local_shopping_requests(
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["local_shopping_requests"]
    statement = select(model)
    if not roles.intersection({"admin", "manager", "staff"}):
        statement = statement.where(model.user_id == user.id)
    result = await session.execute(statement.order_by(model.created_at.desc()).limit(500))
    return {"data": await serialize_local_shopping_requests(session, list(result.scalars()))}


@router.post("/api/shopping/local/requests", status_code=201)
async def api_create_local_shopping_request(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    raw_items = body.get("items")
    items = raw_items if isinstance(raw_items, list) and raw_items else None
    item_descriptions: list[str] = []
    total_quantity = 0
    total_amount = Decimal("0")
    if items is not None:
        for item in items:
            if not isinstance(item, dict):
                raise HTTPException(status_code=422, detail="invalid_item")
            try:
                item_quantity = int(item.get("quantity") or 1)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="invalid_quantity")
            if item_quantity < 1 or item_quantity > 100:
                raise HTTPException(status_code=422, detail="invalid_quantity")
            item_description = _first_text(
                item.get("description"),
                item.get("product_description"),
                item.get("product_name"),
                item.get("product_url"),
                default="منتج محلي",
            )
            item_descriptions.append(item_description)
            total_quantity += item_quantity
            total_amount += _money_from_payload(
                item.get("amount")
                or item.get("unit_price")
                or item.get("estimated_price")
                or 0
            ) * item_quantity
        quantity = total_quantity
        description = _first_text(
            body.get("product_description"),
            body.get("description"),
            ", ".join(item_descriptions[:3]),
            default="طلب تسوق محلي متعدد المنتجات",
        )
        amount = total_amount
    else:
        description = _first_text(body.get("product_description"), body.get("description"))
        if len(description) < 10:
            raise HTTPException(status_code=422, detail="product_description_required")
        try:
            quantity = int(body.get("quantity") or 1)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid_quantity")
        if quantity < 1 or quantity > 100:
            raise HTTPException(status_code=422, detail="invalid_quantity")
        amount = _money_from_payload(body.get("amount") or body.get("estimated_amount") or 0)
    payload = {
        **body,
        "user_id": user.id,
        "status": "pending",
        "description": description,
        "quantity": quantity,
        "amount": amount,
    }
    row = await _api_create(session, "local_shopping_requests", payload, user)
    await session.commit()
    return {"data": row}


@router.post("/api/shopping/international/orders", status_code=201)
async def api_create_international_shopping_order(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=422, detail="items_required")
    total = Decimal("0")
    descriptions: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="invalid_item")
        try:
            quantity = int(item.get("quantity") or 1)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid_quantity")
        if quantity < 1:
            raise HTTPException(status_code=422, detail="invalid_quantity")
        unit_price = _money_from_payload(item.get("unit_price") or item.get("estimated_price") or 0)
        total += unit_price * quantity
        descriptions.append(_first_text(item.get("product_name"), item.get("url"), default="منتج دولي"))
    description = _first_text(body.get("notes"), ", ".join(descriptions[:3]), default="طلب شراء دولي")
    row = await _api_create(
        session,
        "international_orders",
        {
            **body,
            "user_id": user.id,
            "status": "pending",
            "description": description[:500],
            "amount": total,
        },
        user,
    )
    await session.commit()
    return {"data": row}


@router.get("/api/admin-shopping/international-orders")
async def api_admin_international_orders(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    rows = await _rows(session, "international_orders", limit=500)
    orders = await _international_order_payloads(session, rows)
    profiles_by_id = {
        str(profile["id"]): profile
        for profile in (order.get("profiles") for order in orders)
        if isinstance(profile, dict) and profile.get("id")
    }
    return {"data": {"orders": orders, "profiles": list(profiles_by_id.values())}}


@router.get("/api/admin-shopping/purchases")
async def api_admin_purchases(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "international_purchases", limit=500)]}


@router.get("/api/admin-shopping/purchases-unlinked-orders")
async def api_admin_purchases_unlinked(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    rows = [
        row
        for row in await _rows(session, "international_orders", limit=500)
        if not _international_purchase_link_id(row)
        and str(row.status or "").lower() in {"new", "pending", "reviewing"}
    ]
    return {"data": await _international_order_payloads(session, rows)}


@router.get("/api/admin-shopping/purchases/{purchase_id}")
async def api_admin_purchase_details(purchase_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    purchase_model = MODEL_BY_TABLE["international_purchases"]
    purchase = await session.get(purchase_model, purchase_id)
    if purchase is None or purchase.deleted_at is not None:
        raise HTTPException(status_code=404, detail="international_purchase_not_found")

    order_rows = [
        row
        for row in await _rows(session, "international_orders", limit=500)
        if _international_purchase_link_id(row) == str(purchase_id)
    ]
    orders = await _international_order_payloads(session, order_rows)
    items: list[dict[str, Any]] = []
    for order in orders:
        order_items = order.get("items") if isinstance(order.get("items"), list) else []
        for index, item in enumerate(order_items):
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    "id": f"{order.get('id')}-{index}",
                    "profiles": order.get("profiles"),
                    "order_id": order.get("id"),
                    "product_name": item.get("product_name") or item.get("notes") or "منتج دولي",
                    "product_url": item.get("product_url") or item.get("url"),
                    "quantity": item.get("quantity") or 1,
                    "size": item.get("size"),
                    "unit_price": item.get("unit_price") or item.get("final_price") or item.get("estimated_price") or 0,
                    "total_customer_cost": item.get("line_total") or item.get("subtotal") or item.get("total_customer_cost") or 0,
                    "currency_code": item.get("currency_code") or item.get("item_currency_code") or order.get("currency_code"),
                }
            )
    return {
        "data": {
            "purchase": serialize_record(purchase),
            "orders": orders,
            "items": items,
        }
    }


@router.get("/api/admin-shopping/local-requests")
async def api_admin_local_requests(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    requests = await _rows(session, "local_shopping_requests", limit=500)
    return {"data": {
        "requests": await serialize_local_shopping_requests(session, requests),
        "merchants": [serialize_record(row) for row in await _rows(session, "local_merchants", limit=500)],
    }}


ORDER_LINK_INTERNATIONAL_KEYS = (
    "linked_international_order_id",
    "linkedInternationalOrderId",
)
ORDER_LINK_LOCAL_KEYS = (
    "linked_local_order_id",
    "linkedLocalOrderId",
)


def _order_link_value(row: Any, keys: tuple[str, ...]) -> str | None:
    extra = dict(getattr(row, "extra_data", None) or {}) if not isinstance(row, dict) else row
    return _first_text(*(extra.get(key) for key in keys)) or None


def _linked_international_ids(
    international_rows: list[Any],
    local_rows: list[Any],
) -> set[str]:
    """Resolve both directions of the persisted one-to-one link contract."""
    local_ids = {str(row.id) for row in local_rows}
    linked_ids: set[str] = set()
    for local_row in local_rows:
        international_id = _order_link_value(local_row, ORDER_LINK_INTERNATIONAL_KEYS)
        if international_id:
            linked_ids.add(international_id)
    for international_row in international_rows:
        local_id = _order_link_value(international_row, ORDER_LINK_LOCAL_KEYS)
        if local_id and local_id in local_ids:
            linked_ids.add(str(international_row.id))
    return linked_ids


async def _unlinked_international_count(session: AsyncSession) -> int:
    international_rows = await _rows(session, "international_orders", limit=1000)
    local_rows = await _rows(session, "orders", limit=2000)
    linked_ids = _linked_international_ids(international_rows, local_rows)
    terminal_statuses = {"cancelled", "canceled", "returned", "refunded"}
    return sum(
        1
        for row in international_rows
        if str(row.status or "").strip().lower() not in terminal_statuses
        and str(row.id) not in linked_ids
    )


@router.get("/api/admin-shopping/order-links/international")
async def api_admin_order_links(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    international_rows = await _rows(session, "international_orders", limit=500)
    local_rows = await _rows(session, "orders", limit=1000)
    linked_ids = _linked_international_ids(international_rows, local_rows)
    linked_by_international: dict[str, dict[str, Any]] = {}
    for local_row in local_rows:
        international_id = _order_link_value(local_row, ORDER_LINK_INTERNATIONAL_KEYS)
        if international_id:
            linked_by_international.setdefault(international_id, serialize_record(local_row))
    local_by_id = {str(row.id): row for row in local_rows}
    for international_row in international_rows:
        local_id = _order_link_value(international_row, ORDER_LINK_LOCAL_KEYS)
        if local_id and local_id in local_by_id:
            linked_by_international.setdefault(
                str(international_row.id),
                serialize_record(local_by_id[local_id]),
            )
    international_payloads = await _international_order_payloads(session, international_rows)
    for payload in international_payloads:
        linked_local = linked_by_international.get(str(payload.get("id")))
        payload["linked_order_id"] = linked_local.get("id") if linked_local else None
        payload["linked_local_order_id"] = linked_local.get("id") if linked_local else None
    return {
        "data": international_payloads,
        "meta": {
            "total": len(international_payloads),
            "linked": len(linked_ids.intersection({str(row.id) for row in international_rows})),
            "unlinked": len(international_payloads) - len(linked_ids.intersection({str(row.id) for row in international_rows})),
        },
    }


@router.get("/api/loyalty/tiers")
async def api_loyalty_tiers(session: AsyncSession = Depends(get_session)):
    return {"data": await LoyaltyTierService.list_real_tiers(session)}


@router.get("/api/loyalty/settings")
async def api_loyalty_settings(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "loyalty_settings", limit=50)]}


INVENTORY_OUT_MOVEMENT_TYPES = frozenset({"out", "remove", "decrease"})


def _inventory_reference_id(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _inventory_quantity(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _enrich_inventory_payload(
    payload: dict[str, Any],
    products: dict[str, dict[str, Any]],
    warehouses: dict[str, dict[str, Any]],
    *,
    movement: bool = False,
) -> dict[str, Any]:
    normalized = dict(payload)
    product_id = _inventory_reference_id(normalized, "product_id", "productId")
    warehouse_id = _inventory_reference_id(normalized, "warehouse_id", "warehouseId")
    if product_id:
        normalized["product_id"] = product_id
    if warehouse_id:
        normalized["warehouse_id"] = warehouse_id
    if product_id and product_id in products:
        normalized["product"] = products[product_id]
    if warehouse_id and warehouse_id in warehouses:
        normalized["warehouse"] = warehouses[warehouse_id]
    if movement:
        movement_type = str(normalized.get("movement_type") or normalized.get("type") or "adjustment").strip().lower()
        quantity = abs(_inventory_quantity(normalized.get("quantity")))
        normalized["movement_type"] = movement_type
        normalized["signed_quantity"] = -quantity if movement_type in INVENTORY_OUT_MOVEMENT_TYPES else quantity
    else:
        normalized["quantity"] = _inventory_quantity(normalized.get("quantity"))
        if normalized.get("min_quantity") in (None, ""):
            product = normalized.get("product") or {}
            normalized["min_quantity"] = _inventory_quantity(product.get("min_stock_quantity"), 5) or 5
    return normalized


async def _inventory_reference_maps(session: AsyncSession) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    product_rows = await _rows(session, "products", limit=5000)
    warehouse_rows = await _rows(session, "warehouses", limit=500)
    products = {
        str(row.id): serialize_record(row)
        for row in product_rows
    }
    warehouses = {
        str(row.id): serialize_record(row)
        for row in warehouse_rows
    }
    return products, warehouses


async def _canonical_inventory_payloads(
    session: AsyncSession,
    warehouse_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build one inventory row per catalog product from the same stock source."""
    product_rows = await _rows(session, "products", limit=5000)
    products, warehouses = await _inventory_reference_maps(session)
    location_payloads = [serialize_record(row) for row in await _rows(session, "inventory_locations", limit=5000)]
    if not location_payloads:
        location_payloads = [serialize_record(row) for row in await _rows(session, "inventory", limit=5000)]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for payload in location_payloads:
        product_id = _inventory_reference_id(payload, "product_id", "productId")
        if not product_id:
            continue
        row_warehouse_id = _inventory_reference_id(payload, "warehouse_id", "warehouseId")
        if warehouse_id and row_warehouse_id != warehouse_id:
            continue
        grouped.setdefault(product_id, []).append(payload)

    result: list[dict[str, Any]] = []
    for product_row in product_rows:
        product_id = str(product_row.id)
        product = products.get(product_id)
        if product is None:
            continue
        rows = grouped.get(product_id, [])
        if rows:
            quantity = sum(_inventory_quantity(row.get("quantity")) for row in rows)
            source = rows[0]
            source_warehouse_id = warehouse_id or _inventory_reference_id(source, "warehouse_id", "warehouseId")
        else:
            quantity = 0 if warehouse_id else _inventory_quantity(product.get("stock_quantity"))
            source = {}
            source_warehouse_id = warehouse_id
        payload = {
            "id": source.get("id") or f"product:{product_id}",
            "product_id": product_id,
            "warehouse_id": source_warehouse_id,
            "quantity": quantity,
            "min_quantity": product.get("min_stock_quantity") or 5,
            "product": product,
        }
        if source_warehouse_id and source_warehouse_id in warehouses:
            payload["warehouse"] = warehouses[source_warehouse_id]
        result.append(_enrich_inventory_payload(payload, products, warehouses))
    return result


def _canonical_inventory_stock_counts(payloads: list[dict[str, Any]]) -> dict[str, int]:
    """Return dashboard stock counts from the same projection as inventory screens."""
    active_rows = [
        payload
        for payload in payloads
        if (payload.get("product") or {}).get("is_active", True) is not False
    ]
    quantities = [_inventory_quantity(payload.get("quantity")) for payload in active_rows]
    return {
        "out_of_stock": sum(1 for quantity in quantities if quantity <= 0),
        "low_stock": sum(1 for quantity in quantities if 0 < quantity <= 5),
    }


@router.get("/api/admin/inventory/warehouses")
async def api_inventory_warehouses(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "warehouses", limit=500)]}


@router.get("/api/admin/inventory/products")
async def api_inventory_products(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "products", limit=500)]}


@router.get("/api/admin/inventory/locations")
async def api_inventory_locations(
    warehouse_id: str | None = Query(default=None, alias="warehouseId"),
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await _canonical_inventory_payloads(session, warehouse_id)}


@router.get("/api/admin/inventory/movements")
async def api_inventory_movements(
    warehouse_id: str | None = Query(default=None, alias="warehouseId"),
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    movement_payloads = [serialize_record(row) for row in await _rows(session, "inventory_movements", limit=500)]
    if warehouse_id:
        movement_payloads = [
            payload
            for payload in movement_payloads
            if _inventory_reference_id(payload, "warehouse_id", "warehouseId") == warehouse_id
        ]
    products, warehouses = await _inventory_reference_maps(session)
    return {
        "data": [
            _enrich_inventory_payload(payload, products, warehouses, movement=True)
            for payload in movement_payloads
        ]
    }


@router.post("/api/admin/inventory/movements", status_code=201)
async def api_create_inventory_movement(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    warehouse_id = str(body.get("warehouse_id") or body.get("warehouseId") or "")
    if not warehouse_id:
        raise HTTPException(status_code=422, detail="warehouse_required")
    try:
        product_id = uuid.UUID(str(body.get("product_id") or body.get("productId")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="product_required") from exc
    try:
        quantity = int(body.get("quantity") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="quantity_invalid") from exc
    if quantity <= 0:
        raise HTTPException(status_code=422, detail="quantity_must_be_positive")
    movement_type = str(body.get("movement_type") or body.get("type") or "in").strip().lower()
    if movement_type not in {"in", "out", "remove", "decrease", "adjustment"}:
        raise HTTPException(status_code=422, detail="movement_type_invalid")
    product = await session.get(Product, product_id, with_for_update=True)
    if product is None or product.deleted_at is not None:
        raise HTTPException(status_code=404, detail="product_not_found")
    loc_model = MODEL_BY_TABLE["inventory_locations"]
    clauses = (loc_model.extra_data["warehouse_id"].astext == warehouse_id, loc_model.extra_data["product_id"].astext == str(product_id))
    locations = await _rows(session, "inventory_locations", clauses=clauses, limit=1)
    location = locations[0] if locations else loc_model(name=f"{warehouse_id}:{product_id}", status="active", is_active=True, extra_data={"warehouse_id": warehouse_id, "product_id": str(product_id), "quantity": 0})
    if not locations:
        session.add(location)
        await session.flush()
    extra = dict(location.extra_data or {})
    current = int(extra.get("quantity") or 0)
    delta = -quantity if movement_type in {"out", "remove", "decrease"} else quantity
    if current + delta < 0:
        raise HTTPException(status_code=409, detail="insufficient_inventory")
    extra["quantity"] = current + delta
    location.extra_data = extra
    all_locations = await _rows(
        session,
        "inventory_locations",
        clauses=(loc_model.extra_data["product_id"].astext == str(product_id),),
        limit=5000,
    )
    product.stock_quantity = sum(_inventory_quantity(dict(row.extra_data or {}).get("quantity")) for row in all_locations)
    row = await _api_create(session, "inventory_movements", {
        "product_id": product_id,
        "quantity": quantity,
        "type": movement_type,
        "status": "completed",
        "notes": body.get("notes") or "",
        "warehouse_id": warehouse_id,
        "inventory_location_id": str(location.id),
    }, staff)
    await session.commit()
    return {"data": row}


@router.get("/api/catalog/admin/options/colors")
async def api_color_options(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "color_options", limit=500)]}


@router.post("/api/catalog/admin/options/colors", status_code=201)
async def api_create_color_option(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("color_options", request, session, staff)


@router.patch("/api/catalog/admin/options/colors/{record_id}")
async def api_update_color_option(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("color_options", request, session, staff, record_id, "update")


@router.delete("/api/catalog/admin/options/colors/{record_id}")
async def api_delete_color_option(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("color_options", request, session, staff, record_id, "delete")


@router.get("/api/catalog/admin/options/sizes")
async def api_size_options(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [_serialize_size_option(row) for row in await _rows(session, "size_options", limit=500)]}


def _size_option_values(raw: Any) -> list[str]:
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


def _normalize_size_option_body(body: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    values = dict(body)
    category = values.get("category_type") or values.get("category_id") or values.get("category")
    if category is not None:
        category = str(category).strip()
        if not category:
            raise HTTPException(status_code=422, detail={"code": "size_category_required", "message": "يجب اختيار فئة المقاسات"})
        values["category_type"] = category
        values.setdefault("name", category)
        values.setdefault("code", category)
    if "sizes" in values:
        sizes = _size_option_values(values.get("sizes"))
        if not sizes:
            raise HTTPException(status_code=422, detail={"code": "size_values_required", "message": "أدخل مقاسًا واحدًا على الأقل"})
        values["sizes"] = sizes
    elif not partial:
        raise HTTPException(status_code=422, detail={"code": "size_values_required", "message": "أدخل مقاسًا واحدًا على الأقل"})
    if not partial:
        values.setdefault("status", "active")
        values.setdefault("is_active", True)
    return values


def _serialize_size_option(row: Any) -> dict[str, Any]:
    payload = serialize_record(row)
    return _serialize_size_option_payload(payload)


def _serialize_size_option_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    category = (
        payload.get("category_type")
        or payload.get("category_id")
        or payload.get("category")
        or payload.get("name")
        or payload.get("code")
        or ""
    )
    sizes = _size_option_values(
        payload.get("sizes")
        or payload.get("size_values")
        or payload.get("values")
        or payload.get("options")
    )
    payload["category_type"] = str(category).strip()
    payload["sizes"] = sizes
    return payload


@router.post("/api/catalog/admin/options/sizes", status_code=201)
async def api_create_size_option(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _api_create(session, "size_options", _normalize_size_option_body(await request.json()), staff)
    await session.commit()
    return {"data": _serialize_size_option_payload(row)}


@router.patch("/api/catalog/admin/options/sizes/{record_id}")
async def api_update_size_option(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _api_update(session, "size_options", record_id, _normalize_size_option_body(await request.json(), partial=True), staff)
    await session.commit()
    return {"data": _serialize_size_option_payload(row)}


@router.delete("/api/catalog/admin/options/sizes/{record_id}")
async def api_delete_size_option(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("size_options", request, session, staff, record_id, "delete")


@router.get("/api/catalog/admin/categories")
async def api_admin_list_categories(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "categories", limit=5000)]}


@router.get("/api/catalog/admin/brands")
async def api_admin_list_brands(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "brands", limit=5000)]}


@router.get("/api/catalog/admin/products")
async def api_admin_list_products(staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.view")
    product_rows = await _rows(session, "products", limit=5000)
    # The inventory screen and the product screen must use one stock source.
    # Legacy data can contain a stale products.stock_quantity alongside a
    # current inventory_locations quantity. Reuse the canonical inventory
    # projection here so both admin views show the same number without
    # mutating production data during a read.
    canonical_inventory = await _canonical_inventory_payloads(session)
    inventory_quantity_by_product = {
        str(item.get("product_id")): _inventory_quantity(item.get("quantity"))
        for item in canonical_inventory
        if item.get("product_id")
    }
    data = []
    for row in product_rows:
        payload = serialize_record(row)
        product_id = str(payload.get("id") or "")
        if product_id in inventory_quantity_by_product:
            payload["stock_quantity"] = inventory_quantity_by_product[product_id]
        data.append(payload)
    return {"data": data}


@router.post("/api/catalog/admin/products/{product_id}/variants", status_code=201)
async def api_admin_create_product_variant(product_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.update")
    body = normalize_product_mutation_values(_normalize_catalog_mutation_input(await request.json()), partial=True)
    row = await _api_create(session, "product_variants", {**body, "product_id": product_id}, staff)
    await session.commit()
    return {"data": row}


@router.delete("/api/catalog/admin/variants/{variant_id}")
async def api_admin_delete_product_variant(variant_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.delete")
    return await _create_update_delete_resource("product_variants", request, session, staff, variant_id, "delete")


@router.get("/api/catalog/admin/currencies")
async def api_admin_list_currencies(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "currencies", limit=500)]}


@router.get("/api/admin/global-sites")
async def api_list_global_sites(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "global_sites", limit=500)]}


@router.get("/api/admin/local-merchants")
async def api_list_local_merchants(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "local_merchants", limit=500)]}


@router.get("/api/admin/warehouses")
async def api_list_warehouses(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "warehouses", limit=500)]}


@router.get("/api/admin/couriers")
async def api_list_admin_couriers(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "couriers", limit=500)]}


@router.get("/api/suppliers")
async def api_list_suppliers(
    type: str | None = None,
    active: bool | None = None,
    user: User | None = Depends(optional_user),
    session: AsyncSession = Depends(get_session),
):
    roles = set(await roles_for(session, user.id)) if user else set()
    if roles.intersection({"admin", "manager", "finance", "logistics", "staff", "employee"}):
        rows = [serialize_record(row) for row in await _rows(session, "suppliers", limit=500)]
        return {"data": rows}
    requested_type = str(type or "").lower()
    if requested_type and requested_type not in {"merchant", "merchants", "partner", "partners"}:
        raise HTTPException(status_code=403, detail="public_supplier_scope_denied")
    storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    product_model = MODEL_BY_TABLE["products"]
    count_result = await session.execute(
        select(product_model.partner_id, func.count(product_model.id))
        .where(*public_product_clauses(product_model))
        .group_by(product_model.partner_id)
    )
    product_counts = {
        partner_id: int(count or 0)
        for partner_id, count in count_result.all()
    }
    result = await session.execute(
        select(storefront_model)
        .where(storefront_model.deleted_at.is_(None), storefront_model.is_active.is_(True))
        .order_by(storefront_model.created_at.desc())
        .limit(500)
    )
    rows = []
    main_store_count = int(product_counts.get(None, 0) or 0)
    if main_store_count > 0 and active is not True:
        rows.append(public_main_storefront_response(products_count=main_store_count))
    for storefront in result.scalars():
        partner_values = [value for value in (storefront.partner_id, storefront.user_id) if value]
        products_count = sum(int(product_counts.get(value, 0) or 0) for value in partner_values)
        if products_count <= 0:
            continue
        rows.append(public_storefront_response(storefront, products_count=products_count))
    # A partner id without a real storefront record is not a customer-facing
    # store. Do not invent a UUID-based name for it in the public catalog.
    return {"data": rows}


@router.get("/api/marketing/coupons")
async def api_list_coupons(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "coupons", limit=500)]}


@router.get("/api/marketing/campaigns/active")
async def api_public_active_campaigns(type: str | None = None, session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["marketing_campaigns"]
    now_text = datetime.now(timezone.utc).isoformat()
    clauses = [
        model.deleted_at.is_(None),
        model.status.in_(("active", "published", "completed")),
    ]
    if type:
        clauses.append(or_(model.extra_data["campaign_type"].astext == type, model.extra_data["type"].astext == type))
    result = await session.execute(select(model).where(*clauses).order_by(model.created_at.desc()).limit(50))
    rows = []
    for row in result.scalars():
        extra = dict(row.extra_data or {})
        starts_at = extra.get("starts_at") or extra.get("scheduled_at")
        ends_at = extra.get("ends_at")
        if starts_at and str(starts_at) > now_text:
            continue
        if ends_at and str(ends_at) < now_text:
            continue
        rows.append({
            "id": str(row.id),
            "campaign_type": extra.get("campaign_type") or extra.get("type") or "promo_notification",
            "title": row.title,
            "content": row.message,
            "message": row.message,
            "is_active": True,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "settings": extra.get("settings") or {},
            "metrics": extra.get("metrics") or {},
        })
    return {"data": rows}


@router.get("/api/marketing/campaigns")
async def api_list_campaigns(
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await CampaignService().list(session, roles=roles, limit=500)}


@router.get("/api/finance/employee-payments")
async def api_list_employee_payments(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "employee_payments", limit=500)]}


def _general_expense_payload(value: Any) -> dict[str, Any]:
    payload = serialize_record(value) if hasattr(value, "__table__") else dict(value or {})
    category = str(
        payload.get("expense_category")
        or payload.get("category")
        or payload.get("type")
        or "other"
    ).strip()[:64] or "other"
    payload["expense_category"] = category
    payload.setdefault("currency_code", "YER")
    payload.setdefault(
        "expense_date",
        payload.get("created_at") or datetime.now(timezone.utc).isoformat(),
    )
    payload.setdefault("description", "")
    payload["is_recurring"] = bool(payload.get("is_recurring", False))
    if not payload.get("voucher_number"):
        payload["voucher_number"] = f"EXP-{str(payload.get('id') or uuid.uuid4())[:8].upper()}"
    return payload


@router.get("/api/finance/expenses")
async def api_list_expenses(
    category: str | None = Query(None),
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["general_expenses"]
    statement = select(model).where(model.deleted_at.is_(None)).order_by(model.created_at.desc()).limit(500)
    normalized_category = str(category or "").strip()
    if normalized_category and "type" in model.__table__.c:
        statement = statement.where(
            or_(
                model.type == normalized_category,
                model.extra_data["expense_category"].astext == normalized_category,
            )
        )
    result = await session.execute(statement)
    return {"data": [_general_expense_payload(row) for row in result.scalars()]}


def _financial_voucher_payload(value: Any) -> dict[str, Any]:
    payload = serialize_record(value) if hasattr(value, "__table__") else dict(value or {})
    extra_data = payload.get("extra_data")
    if isinstance(extra_data, dict):
        payload = {**extra_data, **payload}
    raw_type = str(payload.get("voucher_type") or payload.get("type") or "receipt").strip().lower()
    voucher_type = raw_type if raw_type in {"receipt", "payment"} else "receipt"
    payload["voucher_type"] = voucher_type
    payload["type"] = voucher_type
    payload.setdefault("currency_code", "YER")
    payload.setdefault("issued_date", payload.get("created_at") or datetime.now(timezone.utc).isoformat())
    if not payload.get("voucher_number"):
        prefix = "RV" if voucher_type == "receipt" else "PV"
        date_part = str(payload.get("issued_date") or datetime.now(timezone.utc).isoformat())[:10].replace("-", "")
        payload["voucher_number"] = f"{prefix}-{date_part}-{str(payload.get('id') or uuid.uuid4())[:8].upper()}"
    return payload


@router.get("/api/finance/vouchers")
async def api_list_vouchers(
    voucher_type: str | None = Query(None, alias="type"),
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    normalized_type = str(voucher_type or "").strip().lower()
    if normalized_type and normalized_type not in {"receipt", "payment"}:
        raise HTTPException(status_code=422, detail="invalid_voucher_type")
    model = MODEL_BY_TABLE["financial_vouchers"]
    clauses = ()
    if normalized_type:
        clauses = (
            or_(
                model.type == normalized_type,
                model.extra_data["voucher_type"].astext == normalized_type,
            ),
        )
    rows = await _rows(session, "financial_vouchers", clauses=clauses, limit=500)
    return {"data": [_financial_voucher_payload(row) for row in rows]}


@router.get("/support/tickets")
async def api_list_support_tickets(
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await SupportWorkflowService().list(session, user=staff, roles=roles, limit=500)}


@router.post("/api/operations/operational/alerts/generate")
async def api_post_operational_alerts_generate(staff: User = Depends(require_staff)):
    return {"data": []}


@router.get("/api/operations/shipping/carriers")
async def api_list_shipping_carriers(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "shipping_carriers", limit=500)]}


@router.get("/api/operations/shipping/stages")
async def api_list_shipping_stages(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "shipping_stages", limit=500)]}


@router.get("/api/notifications/admin/recipients")
async def api_admin_notification_recipients(target: str = "all", staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    normalized = target.strip().lower()
    statement = (
        select(User, Profile)
        .outerjoin(Profile, Profile.user_id == User.id)
        .where(User.is_active.is_(True), User.deleted_at.is_(None))
        .limit(500)
    )
    rows = None
    role_map: dict[uuid.UUID, set[str]] = {}
    if normalized in {"customers", "customer", "with_orders"}:
        # Customer accounts created through the current flow have a customer
        # row in user_roles. Older/imported accounts can instead keep the role
        # in profile fields, so resolve both representations consistently.
        result = await session.execute(statement)
        rows = result.all()
        user_ids = [user_row.id for user_row, _ in rows]
        if user_ids:
            role_result = await session.execute(
                select(UserRole.user_id, UserRole.role).where(UserRole.user_id.in_(user_ids))
            )
            for user_id, role in role_result.all():
                normalized_role = _canonical_role(role)
                if normalized_role:
                    role_map.setdefault(user_id, set()).add(normalized_role)
        rows = [
            (user_row, profile_row)
            for user_row, profile_row in rows
            if _notification_recipient_is_customer(profile_row, role_map.get(user_row.id, set()))
        ]
    elif normalized in {"admins", "admin", "staff"}:
        statement = statement.where(
            User.id.in_(select(UserRole.user_id).where(UserRole.role.in_(("admin", "manager", "staff", "employee"))))
        )
    if rows is None:
        rows = (await session.execute(statement)).all()
    return {
        "data": [
            {
                "id": str(user_row.id),
                "user_id": str(user_row.id),
                "email": user_row.email,
                "phone": getattr(profile_row, "phone", None) if profile_row is not None else None,
                "full_name": getattr(profile_row, "full_name", None) if profile_row is not None else None,
            }
            for user_row, profile_row in rows
        ]
    }


def _marketer_payload(value: Any) -> dict[str, Any]:
    item = serialize_record(value) if hasattr(value, "__table__") else dict(value or {})
    extra = dict(getattr(value, "extra_data", None) or {}) if hasattr(value, "__table__") else dict(item.get("extra_data") or {})
    return {
        **item,
        "email": extra.get("email") or item.get("email") or "",
        "referral_code": extra.get("referral_code") or item.get("referral_code") or item.get("code") or "",
        "commission_rate": float(extra.get("commission_rate") or item.get("commission_rate") or 0),
        "total_sales": float(extra.get("total_sales") or item.get("total_sales") or 0),
        "total_commission": float(extra.get("total_commission") or item.get("total_commission") or 0),
        "pending_commission": float(extra.get("pending_commission") or item.get("pending_commission") or 0),
        "is_active": bool(extra.get("is_active", item.get("is_active", item.get("status") != "inactive"))),
        "notes": extra.get("notes") or item.get("notes") or "",
    }


def _normalize_marketer_body(body: Any, *, for_create: bool = False) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="marketer_payload_required")
    values = dict(body)
    name = _first_text(values.get("name"), values.get("full_name"))
    if for_create or "name" in values or "full_name" in values:
        if not name or len(name) < 2:
            raise HTTPException(status_code=422, detail="marketer_name_required")
        values["name"] = name
        values.pop("full_name", None)
    raw_user_id = values.get("user_id") or values.get("userId")
    if raw_user_id is not None:
        values["user_id"] = _uuid(raw_user_id, "user_id")
        values.pop("userId", None)
    elif for_create:
        raise HTTPException(status_code=422, detail="marketer_user_required")
    if for_create:
        values.setdefault("status", "active")
        if not _first_text(values.get("referral_code"), values.get("referralCode")):
            values["referral_code"] = f"MKT-{uuid.uuid4().hex[:10].upper()}"
    if "referralCode" in values and "referral_code" not in values:
        values["referral_code"] = values.pop("referralCode")
    if "commission_rate" in values or "commissionRate" in values:
        raw_rate = values.get("commission_rate", values.get("commissionRate"))
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid_commission_rate") from exc
        if not 0 <= rate <= 100:
            raise HTTPException(status_code=422, detail="invalid_commission_rate")
        values["commission_rate"] = rate
        values.pop("commissionRate", None)
    if "is_active" in values:
        values["status"] = "active" if bool(values["is_active"]) else "inactive"
    return values


@router.get("/api/marketing/marketers")
async def api_marketers(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [_marketer_payload(row) for row in await _rows(session, "marketers", limit=500)]}


def _marketer_commission_payload(value: Any, marketers_by_user: dict[str, dict[str, Any]]) -> dict[str, Any]:
    item = serialize_record(value)
    extra = dict(getattr(value, "extra_data", {}) or {})
    marketer = marketers_by_user.get(str(item.get("user_id") or ""))
    return {
        **item,
        "marketer_id": marketer.get("id") if marketer else item.get("user_id"),
        "order_amount": float(extra.get("order_amount") or 0),
        "commission_rate": float(extra.get("commission_rate") or 0),
        "commission_amount": float(item.get("amount") or 0),
        "paid_at": extra.get("paid_at"),
        "marketer": {
            "name": marketer.get("name") or "مسوق",
            "phone": marketer.get("phone"),
            "email": marketer.get("email"),
        } if marketer else None,
    }


@router.get("/api/marketing/commissions")
async def api_marketer_commissions(
    marketer_id: uuid.UUID | None = Query(None, alias="marketerId"),
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    marketer_model = MODEL_BY_TABLE["marketers"]
    marketers_by_user = {
        str(marketer.user_id): _marketer_payload(marketer)
        for marketer in list((await session.execute(select(marketer_model).where(marketer_model.deleted_at.is_(None)))).scalars())
    }
    clauses = ()
    if marketer_id is not None:
        marketer = await session.get(marketer_model, marketer_id)
        if marketer is None or marketer.deleted_at is not None:
            return {"data": []}
        clauses = (MODEL_BY_TABLE["marketer_commissions"].user_id == marketer.user_id,)
    data = [_marketer_commission_payload(row, marketers_by_user) for row in await _rows(session, "marketer_commissions", clauses=clauses, limit=500)]
    return {"data": data}


@router.get("/api/finance/orders")
async def api_finance_orders(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    rows = await _rows(session, "orders", limit=500)
    return {"data": await _serialize_orders_with_financials(session, rows)}


@router.get("/api/finance/partner-settlements")
@router.get("/api/finance/partner-settlements/pending")
async def api_partner_settlements(
    request: Request,
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    """Return raw settlements for reports and partner groups for the payment form.

    The accounting payment screen settles all outstanding rows for one partner
    at a time.  Returning the raw rows from the ``/pending`` endpoint made the
    partner selector empty because that screen consumes the grouped camelCase
    contract.  Keep the reporting endpoint backward-compatible and make the
    pending endpoint deterministic, filtered, and safe to settle in one action.
    """
    is_pending = request.url.path.rstrip("/").endswith("/pending")
    settlement_model = MODEL_BY_TABLE["partner_settlements"]
    clauses = (
        (settlement_model.status.in_(("pending", "unpaid")),)
        if is_pending
        else ()
    )
    rows = await _rows(session, "partner_settlements", clauses=clauses, limit=500)
    serialized = [serialize_record(row) for row in rows]
    if not is_pending:
        return {"data": serialized}

    # Resolve a display name even for old settlements that predate the stored
    # partner_name extra field.  partner_id is normally the partner user id,
    # but legacy data can point at a storefront/profile record.
    partner_names: dict[str, str] = {}
    for table in ("partner_storefronts", "partner_profiles", "partner_applications"):
        for partner_row in await _rows(session, table, limit=1000):
            partner_item = serialize_record(partner_row)
            name = _first_text(
                partner_item.get("name"),
                partner_item.get("full_name"),
                partner_item.get("store_name"),
                partner_item.get("email"),
                default="شريك",
            )
            for key in (partner_item.get("id"), partner_item.get("user_id"), partner_item.get("partner_id")):
                if key:
                    partner_names.setdefault(str(key), name)

    def number(*values: Any) -> float:
        for value in values:
            try:
                if value is not None and str(value).strip() != "":
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    grouped: dict[str, dict[str, Any]] = {}
    for row, item in zip(rows, serialized):
        extra = dict(getattr(row, "extra_data", {}) or {})
        partner_id = _first_text(
            item.get("partner_id"),
            extra.get("partner_id"),
            default="",
        )
        if not partner_id:
            continue
        order_amount = number(
            item.get("order_amount"),
            extra.get("order_amount"),
            item.get("total_sales"),
        )
        commission_amount = number(
            item.get("commission_amount"),
            extra.get("commission_amount"),
            item.get("commission"),
        )
        partner_amount = number(
            item.get("partner_amount"),
            extra.get("partner_amount"),
            item.get("amount"),
        )
        commission_rate = number(item.get("commission_rate"), extra.get("commission_rate"))
        partner_share_rate = number(
            item.get("partner_share_rate"),
            extra.get("partner_share_rate"),
            100 - commission_rate if commission_rate else 0,
        )
        aggregate = grouped.setdefault(
            partner_id,
            {
                "partnerId": partner_id,
                "partnerName": _first_text(
                    item.get("partner_name"),
                    extra.get("partner_name"),
                    partner_names.get(partner_id),
                    default="شريك",
                ),
                "totalSales": 0.0,
                "commission": 0.0,
                "commissionRate": commission_rate,
                "partnerShareAmount": 0.0,
                "partnerShareRate": partner_share_rate,
                "settlementIds": [],
            },
        )
        aggregate["totalSales"] += order_amount
        aggregate["commission"] += commission_amount
        aggregate["partnerShareAmount"] += partner_amount
        aggregate["settlementIds"].append(str(row.id))

        # Prefer a calculated rate when multiple rows have different rates,
        # while preserving the explicit configured rate for zero-value rows.
        if aggregate["totalSales"] > 0:
            aggregate["partnerShareRate"] = round(
                aggregate["partnerShareAmount"] / aggregate["totalSales"] * 100,
                4,
            )
            aggregate["commissionRate"] = round(
                aggregate["commission"] / aggregate["totalSales"] * 100,
                4,
            )

    return {"data": list(grouped.values())}


@router.get("/api/finance/marketer-commissions/pending")
async def api_pending_marketer_commissions(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    marketer_model = MODEL_BY_TABLE["marketers"]
    marketers_by_user = {
        str(marketer.user_id): _marketer_payload(marketer)
        for marketer in list((await session.execute(select(marketer_model).where(marketer_model.deleted_at.is_(None)))).scalars())
    }
    rows = await _rows(
        session,
        "marketer_commissions",
        clauses=(MODEL_BY_TABLE["marketer_commissions"].status.in_(("pending", "earned", "approved", "unpaid")),),
        limit=500,
    )
    return {"data": [_marketer_commission_payload(row, marketers_by_user) for row in rows]}


@router.get("/api/finance/summary")
async def api_finance_summary(
    date_from: str | None = Query(None, alias="dateFrom"),
    date_to: str | None = Query(None, alias="dateTo"),
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    revenue = await RevenueRecognitionService.summary(session, start=date_from, end=date_to)
    expense_tables = ("general_expenses", "employee_payments", "partner_payments", "marketer_payments")
    expense_start, expense_end = _date_range(date_from, date_to)

    def expense_date_clauses(table: str) -> tuple[Any, ...]:
        model = MODEL_BY_TABLE[table]
        return tuple(
            clause
            for clause in (
                model.created_at >= expense_start if expense_start else None,
                model.created_at <= expense_end if expense_end else None,
            )
            if clause is not None
        )

    total_expenses = sum(
        (
            await _sum_amount(session, table, *expense_date_clauses(table))
            for table in expense_tables
        ),
        Decimal("0"),
    )
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_income = Decimal(
        (await RevenueRecognitionService.summary(session, start=month_start))["net_revenue"]
    )
    monthly_expenses = sum(
        (
            await _sum_amount(
                session,
                table,
                MODEL_BY_TABLE[table].created_at >= month_start,
            )
            for table in expense_tables
        ),
        Decimal("0"),
    )
    pending_receipt_statuses = ("pending", "uploaded", "reviewing")
    pending_payments = await _sum_amount(
        session,
        "payment_receipts",
        MODEL_BY_TABLE["payment_receipts"].status.in_(pending_receipt_statuses),
    )
    net_income = Decimal(revenue["net_revenue"])
    return {
        "data": {
            # The legacy keys remain available to existing admin screens.
            "orders": float(net_income),
            "recognized_revenue": revenue,
            "payments": float(Decimal(revenue["paid_amount"])),
            "refunds": float(Decimal(revenue["refund_amount"])),
            "expenses": float(total_expenses),
            # These are the stable dashboard contract consumed by the web UI.
            "totalIncome": float(net_income),
            "totalExpenses": float(total_expenses),
            "netBalance": float(net_income - total_expenses),
            "pendingPayments": float(pending_payments),
            "monthlyIncome": float(monthly_income),
            "monthlyExpenses": float(monthly_expenses),
        }
    }


@router.get("/api/finance/today-stats")
async def api_finance_today_stats(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    revenue = await RevenueRecognitionService.summary(session, start=today)
    order_model = MODEL_BY_TABLE["orders"]
    statement = (
        select(order_model)
        .where(order_model.deleted_at.is_(None), order_model.created_at >= today)
        .order_by(order_model.created_at.desc())
        .limit(500)
    )
    orders = await _serialize_orders_with_financials(
        session,
        list((await session.execute(statement)).scalars()),
    )
    return {
        "data": {
            "orders": orders,
            "collected": float(Decimal(revenue["paid_amount"])),
            "recognized_revenue": revenue,
        }
    }


@router.get("/api/finance/marketer-payments")
async def api_marketer_payments(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    rows = await _rows(session, "marketer_payments", limit=500)
    marketer_model = MODEL_BY_TABLE["marketers"]
    marketer_rows = list((await session.execute(select(marketer_model).where(marketer_model.deleted_at.is_(None)))).scalars())
    marketer_by_key = {
        key: _marketer_payload(marketer)
        for marketer in marketer_rows
        for key in (str(marketer.id), str(marketer.user_id))
        if getattr(marketer, "id", None) or getattr(marketer, "user_id", None)
    }
    data = []
    for row in rows:
        item = serialize_record(row)
        extra = dict(item.get("extra_data") or {})
        for field in ("currency_code", "payment_date", "payment_method", "period_from", "period_to", "description", "commissions_paid", "voucher_number"):
            if item.get(field) in (None, "") and field in extra:
                item[field] = extra[field]
        marketer = marketer_by_key.get(str(item.get("user_id") or ""))
        if marketer is not None:
            item["marketer_id"] = marketer.get("id")
            item["marketer"] = {
                "name": marketer.get("name") or "مسوق",
                "phone": marketer.get("phone"),
                "email": marketer.get("email"),
            }
        data.append(item)
    return {"data": data}


@router.get("/api/finance/partner-payments")
async def api_partner_payments(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    rows = await _rows(session, "partner_payments", limit=500)
    data = []
    for row in rows:
        item = serialize_record(row)
        extra = dict(item.get("extra_data") or {})
        for field in ("partner_name", "currency_code", "payment_date", "payment_method", "description", "orders_settled", "voucher_number"):
            if item.get(field) in (None, "") and field in extra:
                item[field] = extra[field]
        data.append(item)
    return {"data": data}


@router.get("/api/finance/reports")
async def api_finance_reports(
    date_from: str | None = Query(None, alias="dateFrom"),
    date_to: str | None = Query(None, alias="dateTo"),
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    ReportGenerationService.require_access(roles)
    return {"data": await RevenueRecognitionService.report_source(session, start=date_from, end=date_to)}


@router.get("/api/operations/operational/days/today")
async def api_operational_day(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await OperationalDayService().today(session)


@router.get("/api/operations/operational/alerts")
async def api_operational_alerts(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "operational_alerts", limit=500)]}


@router.get("/api/operations/operational/blocked-actions")
async def api_operational_blocked(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "security_events", limit=20)]}


@router.get("/api/operations/operational/alert-counts")
async def api_operational_alert_counts(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": {"open": await _count(session, "operational_alerts"), "blocked": await _count(session, "security_events")}}


@router.get("/api/operations/operational/alerts/generate")
async def api_operational_alerts_generate(staff: User = Depends(require_staff)):
    return {"data": []}


@router.get("/api/dashboard/kpis")
async def api_dashboard_kpis(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    yesterday = today - timedelta(days=1)
    order_model = MODEL_BY_TABLE["orders"]
    user_model = MODEL_BY_TABLE["users"]
    product_model = MODEL_BY_TABLE["products"]
    today_revenue_data = await RevenueRecognitionService.summary(session, start=today)
    yesterday_revenue_data = await RevenueRecognitionService.summary(session, start=yesterday, end=today)
    month_revenue_data = await RevenueRecognitionService.summary(session, start=month)
    today_revenue = Decimal(today_revenue_data["net_revenue"])
    yesterday_revenue = Decimal(yesterday_revenue_data["net_revenue"])
    month_revenue = Decimal(month_revenue_data["net_revenue"])
    today_orders = await _count(session, "orders", order_model.created_at >= today)
    inventory_counts = _canonical_inventory_stock_counts(await _canonical_inventory_payloads(session))
    expense_tables = ("general_expenses", "employee_payments", "partner_payments", "marketer_payments")
    monthly_expenses = sum(
        (
            await _sum_amount(session, table, MODEL_BY_TABLE[table].created_at >= month)
            for table in expense_tables
        ),
        Decimal("0"),
    )
    gross_month_revenue = Decimal(month_revenue_data["gross_revenue"])
    collection_rate = (
        float((Decimal(month_revenue_data["paid_amount"]) / gross_month_revenue) * 100)
        if gross_month_revenue > 0
        else 0.0
    )
    return {"data": {
        "revenue": {"today": float(today_revenue), "yesterday": float(yesterday_revenue), "thisMonth": float(month_revenue), "lastMonth": 0, "trend": float(today_revenue - yesterday_revenue)},
        "orders": {"today": today_orders, "pending": await _count(session, "orders", order_model.status.in_(("pending", "new"))), "processing": await _count(session, "orders", order_model.status == "processing"), "completed": await _count(session, "orders", order_model.status.in_(("delivered", "completed"))), "trend": 0},
        "customers": {"total": await _customer_count(session), "new_today": await _customer_count(session, user_model.created_at >= today), "new_this_month": await _customer_count(session, user_model.created_at >= month), "trend": 0},
        "products": {"total": await _count(session, "products"), "low_stock": inventory_counts["low_stock"], "out_of_stock": inventory_counts["out_of_stock"], "pending_approval": await _count(session, "products", product_model.approval_status.in_(("pending", "reviewing")))},
        "payments": {"pending_amount": float(await _sum_amount(session, "payment_receipts", MODEL_BY_TABLE["payment_receipts"].status.in_(("pending", "uploaded", "reviewing")))), "pending_count": await _count(session, "payment_receipts", MODEL_BY_TABLE["payment_receipts"].status.in_(("pending", "uploaded", "reviewing"))), "collected_today": float(await _sum_amount(session, "order_payments", MODEL_BY_TABLE["order_payments"].created_at >= today)), "collection_rate": round(collection_rate, 2)},
        "expenses": {"today": float(await _sum_amount(session, "general_expenses", MODEL_BY_TABLE["general_expenses"].created_at >= today)), "thisMonth": float(monthly_expenses), "general": float(await _sum_amount(session, "general_expenses")), "employees": float(await _sum_amount(session, "employee_payments")), "partners": float(await _sum_amount(session, "partner_payments")), "marketers": float(await _sum_amount(session, "marketer_payments")), "netProfit": float(month_revenue - monthly_expenses)},
        "partners": {"total": await _count(session, "partner_storefronts"), "active": await _count(session, "partner_storefronts", MODEL_BY_TABLE["partner_storefronts"].status.in_(("active", "approved"))), "pending_settlements": await _count(session, "partner_settlements", MODEL_BY_TABLE["partner_settlements"].status.in_(("pending", "unpaid")))},
    }}


@router.get("/api/analytics/activity/access")
async def api_activity_access(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": []}


@router.get("/api/analytics/activity/audit")
async def api_activity_audit(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": []}


@router.get("/api/reviews/store/public")
async def api_store_reviews_public(session: AsyncSession = Depends(get_session)):
    return {"data": await fetch_public_store_reviews(session)}


@router.get("/api/reviews/store/mine")
async def api_store_reviews_mine(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return {"data": await fetch_user_store_review(session, user.id)}


@router.get("/api/reviews/store/admin")
async def api_store_reviews_admin(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "store_reviews", limit=500)]}


@router.get("/api/dashboard/sales-forecasts")
async def api_sales_forecasts(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "sales_forecasts", limit=500)]}


@router.post("/api/dashboard/sales-forecasts/generate")
async def api_generate_sales_forecasts(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    """Generate a small, persisted sales forecast from recent order history.

    The resource schema stores the forecast-specific fields in ``extra_data`` so
    this endpoint remains compatible with installations that use the generic
    resource table definitions.
    """
    forecast_model = MODEL_BY_TABLE["sales_forecasts"]
    since = datetime.now(timezone.utc) - timedelta(days=30)
    excluded_statuses = ("cancelled", "canceled", "rejected", "refunded")
    summary = await session.execute(
        select(
            func.coalesce(func.sum(Order.total), 0),
            func.count(Order.id),
        ).where(
            Order.created_at >= since,
            Order.status.notin_(excluded_statuses),
            Order.deleted_at.is_(None),
        )
    )
    total_revenue, total_orders = summary.one()
    historical_revenue = Decimal(str(total_revenue or 0))
    historical_orders = int(total_orders or 0)
    average_daily_revenue = historical_revenue / Decimal("30")
    average_daily_orders = Decimal(historical_orders) / Decimal("30")
    confidence = 40 if historical_orders == 0 else min(95, 50 + historical_orders * 2)

    # A regenerate action replaces only generated forecast rows. It does not
    # touch orders, payments, products, or any user-visible business data.
    await session.execute(
        delete(forecast_model).where(
            forecast_model.type == "sales_forecast",
            forecast_model.status == "generated",
        )
    )

    generated: list[Any] = []
    start_date = datetime.now(timezone.utc).date()
    for offset in range(1, 8):
        forecast_date = start_date + timedelta(days=offset)
        predicted_revenue = (average_daily_revenue * Decimal(str(offset))).quantize(Decimal("0.01"))
        predicted_orders = int((average_daily_orders * Decimal(str(offset))).quantize(Decimal("1")))
        row = forecast_model(
            status="generated",
            type="sales_forecast",
            amount=predicted_revenue,
            description="تنبؤ مبيعات مبني على آخر 30 يومًا",
            extra_data={
                "forecast_date": forecast_date.isoformat(),
                "predicted_revenue": float(predicted_revenue),
                "predicted_orders": predicted_orders,
                "confidence_score": confidence,
                "model_version": "rolling-average-v1",
                "metadata": {
                    "historical_days": 30,
                    "historical_revenue": float(historical_revenue),
                    "historical_orders": historical_orders,
                },
            },
        )
        session.add(row)
        generated.append(row)

    await session.commit()
    return {"data": [serialize_record(row) for row in generated]}


@router.get("/api/dashboard/sensitive-data-changes")
async def api_sensitive_data_changes(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "security_events", limit=500)]}


@router.get("/api/suppliers/counts/products")
async def api_supplier_product_counts(session: AsyncSession = Depends(get_session)):
    product_model = MODEL_BY_TABLE["products"]
    counts: dict[str, int] = {"main-store": 0}
    result = await session.execute(
        select(product_model.supplier_id, product_model.partner_id, func.count(product_model.id))
        .where(*public_product_clauses(product_model))
        .group_by(product_model.supplier_id, product_model.partner_id)
    )
    for supplier_id, partner_id, count in result.all():
        safe_count = int(count or 0)
        if supplier_id:
            counts[str(supplier_id)] = counts.get(str(supplier_id), 0) + safe_count
        if partner_id:
            counts[str(partner_id)] = counts.get(str(partner_id), 0) + safe_count
        if supplier_id is None and partner_id is None:
            counts["main-store"] += safe_count
    return {"data": counts}


@router.get("/api/suppliers/counts/orders")
async def api_supplier_order_counts(staff: User = Depends(require_staff)):
    return {"data": {}}


@router.post("/api/analytics/events", status_code=202)
async def api_create_analytics_events(
    request: Request,
    user: User | None = Depends(optional_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    payloads = body if isinstance(body, list) else [body]
    analytics_model = MODEL_BY_TABLE["analytics_events"]
    columns = analytics_model.__table__.c
    created = 0
    for payload in payloads[:50]:
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("event_type") or "").strip()[:80]
        page_path = str(payload.get("page_path") or "").strip()[:500]
        if not event_type or not page_path:
            continue
        values: dict[str, Any] = {}
        if "event_type" in columns:
            values["event_type"] = event_type
        if "page_path" in columns:
            values["page_path"] = page_path
        if "metadata" in columns:
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            values["metadata"] = _jsonable(metadata)
        if "session_id" in columns:
            values["session_id"] = str(payload.get("session_id") or "")[:120] or None
        if "user_id" in columns:
            values["user_id"] = user.id if user else None
        if "created_at" in columns:
            values["created_at"] = datetime.now(timezone.utc)
        row = analytics_model(**values)
        session.add(row)
        created += 1
    if created:
        await session.commit()
    return {"accepted": created}


@router.get("/api/analytics/events")
async def api_analytics_events(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "analytics_events", limit=500)]}


@router.patch("/api/loyalty/admin/tiers/{tier_id}")
async def api_loyalty_admin_update_tier(tier_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("loyalty_tiers", request, session, staff, tier_id, "update")


@router.post("/api/loyalty/admin/tiers", status_code=201)
async def api_loyalty_admin_create_tier(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if not isinstance(body, dict) or len(str(body.get("name") or "").strip()) < 2:
        raise HTTPException(status_code=422, detail="loyalty_tier_name_required")
    body = {
        **body,
        "name": str(body.get("name") or "").strip(),
        "status": "active",
        "is_active": True,
    }
    row = await _api_create(session, "loyalty_tiers", body, staff)
    await session.commit()
    return {"data": row}


@router.patch("/api/loyalty/admin/settings")
async def api_loyalty_admin_update_settings(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE["loyalty_settings"]
    rows = await _rows(session, "loyalty_settings", clauses=(model.name == "default",), limit=1)
    row = rows[0] if rows else model(name="default", status="active", is_active=True, extra_data={})
    if not rows:
        session.add(row)
    row.extra_data = _jsonable(body)
    await session.commit()
    return {"data": serialize_record(row)}


@router.post("/api/loyalty/admin/points", status_code=201)
async def api_loyalty_admin_points(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    user_id = uuid.UUID(str(body.get("user_id") or body.get("userId")))
    points = Decimal(str(body.get("points") or body.get("amount") or 0))
    loyalty_model = MODEL_BY_TABLE["user_loyalty"]
    rows = await _rows(session, "user_loyalty", clauses=(loyalty_model.user_id == user_id,), limit=1)
    loyalty = rows[0] if rows else loyalty_model(user_id=user_id, status="active", balance=0, extra_data={})
    if not rows:
        session.add(loyalty)
        await session.flush()
    loyalty.balance = Decimal(str(loyalty.balance or 0)) + points
    row = await _api_create(session, "points_transactions", {
        "user_id": user_id,
        "amount": points,
        "type": body.get("type") or "adjustment",
        "description": body.get("description") or "",
    }, staff)
    await session.commit()
    return {"data": row}


def _public_product_review_filters(product_id: uuid.UUID):
    review_model = MODEL_BY_TABLE["product_reviews"]
    columns = review_model.__table__.c
    clauses = [columns.product_id == product_id]
    if "deleted_at" in columns:
        clauses.append(columns.deleted_at.is_(None))
    if "status" in columns:
        clauses.append(columns.status.in_(("approved", "active", "published", "visible", "live")))
    if "is_approved" in columns:
        clauses.append(columns.is_approved.is_(True))
    return review_model, columns, clauses


REVIEW_APPROVED_STATUSES = ("approved", "active", "published", "visible", "live", "accepted", "approve", "accept")
REVIEW_ORDER_STATUSES = ("delivered", "completed", "received")
MAX_REVIEW_IMAGES = 5
MAX_REVIEW_COMMENT_LENGTH = 1000


def _normalize_store_review_status_payload(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid_review_status_payload")
    values = dict(body)
    if "is_approved" in values or "is_rejected" in values:
        approved = values.get("is_approved") is True or str(values.get("is_approved") or "").strip().lower() in {"true", "1", "yes"}
        rejected = values.get("is_rejected") is True or str(values.get("is_rejected") or "").strip().lower() in {"true", "1", "yes"}
        values["status"] = "rejected" if rejected else "approved" if approved else "pending"
    return values


def _review_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _normalize_review_images(value: Any, request: Request | None = None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="review_images_must_be_a_list")
    if len(value) > MAX_REVIEW_IMAGES:
        raise HTTPException(status_code=422, detail="review_images_limit_exceeded")

    settings = get_settings()
    allowed_origins = {
        str(settings.api_base_url).rstrip("/").lower(),
        str(settings.r2_public_base_url).rstrip("/").lower(),
    }
    if request is not None:
        allowed_origins.add(str(request.base_url).rstrip("/").lower())

    normalized: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail="invalid_review_image")
        candidate = raw.strip()
        if not candidate or len(candidate) > 1000:
            raise HTTPException(status_code=422, detail="invalid_review_image")
        if candidate.lower().startswith(("data:", "blob:", "file:", "javascript:")):
            raise HTTPException(status_code=422, detail="invalid_review_image")
        if candidate.startswith("uploads/"):
            candidate = f"/{candidate}"
        if candidate.startswith("/uploads/"):
            if not FileStorage.is_public_relative_path(candidate[len("/uploads/") :]):
                raise HTTPException(status_code=422, detail="invalid_review_image")
        elif candidate.startswith("http://") or candidate.startswith("https://"):
            parsed = urlparse(candidate)
            origin = f"{parsed.scheme}://{parsed.netloc}".lower()
            is_allowed_api_url = origin in allowed_origins and parsed.path.startswith("/uploads/")
            is_allowed_r2_url = (
                bool(str(settings.r2_public_base_url).strip())
                and origin == str(settings.r2_public_base_url).rstrip("/").lower()
            )
            if not (is_allowed_api_url or is_allowed_r2_url) or parsed.query or parsed.fragment:
                raise HTTPException(status_code=422, detail="invalid_review_image")
        else:
            raise HTTPException(status_code=422, detail="invalid_review_image")
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _notification_recipient_is_customer(profile: Profile | None, explicit_roles: set[str]) -> bool:
    """Use the same customer role fallback as authentication for legacy accounts."""
    if explicit_roles:
        return "customer" in explicit_roles
    if profile is None:
        return False
    profile_roles = _roles_from_any(profile.classification)
    profile_roles.update(_roles_from_any(profile.extra_data))
    return "customer" in profile_roles


def _review_input_values(body: Any, request: Request) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="review_payload_must_be_an_object")
    raw_rating = body.get("rating")
    if isinstance(raw_rating, bool):
        raise HTTPException(status_code=422, detail="rating_must_be_between_1_and_5")
    try:
        rating = int(raw_rating)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="rating_must_be_between_1_and_5")
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=422, detail="rating_must_be_between_1_and_5")
    comment = _review_text(body.get("comment", body.get("body", "")))
    if len(comment) > MAX_REVIEW_COMMENT_LENGTH:
        raise HTTPException(status_code=422, detail="review_comment_limit_exceeded")
    images = _normalize_review_images(body.get("review_images", body.get("reviewImages", [])), request)
    return {"rating": rating, "comment": comment, "review_images": images}


async def _public_review_product(session: AsyncSession, product_id: uuid.UUID) -> Product:
    result = await session.execute(
        select(Product).where(Product.id == product_id, *public_product_clauses(Product))
    )
    return validate_public_product_or_404(result.scalar_one_or_none())


async def _eligible_review_order_id(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    product_id: uuid.UUID,
) -> uuid.UUID | None:
    result = await session.execute(
        select(Order.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.user_id == user_id,
            OrderItem.product_id == product_id,
            Order.deleted_at.is_(None),
            func.lower(func.coalesce(Order.payment_status, "")) == "paid",
            func.lower(func.coalesce(Order.status, "")).in_(REVIEW_ORDER_STATUSES),
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _review_eligibility(
    session: AsyncSession,
    *,
    user: User,
    product_id: uuid.UUID,
) -> dict[str, Any]:
    await _public_review_product(session, product_id)
    review_model, columns, _ = _public_product_review_filters(product_id)
    existing_id = (
        await session.execute(
            select(review_model.id)
            .where(
                columns.user_id == user.id,
                columns.product_id == product_id,
                columns.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_id:
        return {
            "can_review": False,
            "reason": "already_reviewed",
            "message": "لديك تقييم سابق لهذا المنتج.",
            "order_id": None,
            "review_id": str(existing_id),
            "has_review": True,
        }

    eligible_order_id = await _eligible_review_order_id(
        session, user_id=user.id, product_id=product_id
    )
    if eligible_order_id:
        return {
            "can_review": True,
            "reason": "eligible",
            "message": "يمكنك الآن تقييم المنتج بعد استلام طلبك.",
            "order_id": str(eligible_order_id),
            "eligible_order_id": str(eligible_order_id),
            "has_review": False,
        }

    any_order = (
        await session.execute(
            select(Order.id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(
                Order.user_id == user.id,
                OrderItem.product_id == product_id,
                Order.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if any_order:
        return {
            "can_review": False,
            "reason": "not_delivered",
            "message": "سيظهر التقييم بعد دفع الطلب واستلامه.",
            "order_id": None,
            "has_review": False,
        }
    return {
        "can_review": False,
        "reason": "not_purchased",
        "message": "يمكنك تقييم المنتج بعد شرائه واستلامه.",
        "order_id": None,
        "has_review": False,
    }


async def _product_review_response(session: AsyncSession, row: Any) -> dict[str, Any]:
    raw = serialize_record(row)
    status = str(raw.get("status") or "").lower()
    profile = (
        await session.execute(
            select(Profile)
            .where(Profile.user_id == getattr(row, "user_id", None), Profile.deleted_at.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    stored_images = getattr(row, "review_images", None)
    images = [item for item in stored_images if isinstance(item, str)] if isinstance(stored_images, list) else []
    return {
        "id": str(row.id),
        "product_id": str(row.product_id),
        "rating": int(getattr(row, "rating", 0) or 0),
        "comment": _review_text(getattr(row, "comment", None) or getattr(row, "body", None)) or None,
        "review_images": images,
        "is_approved": bool(getattr(row, "is_approved", None)) or status in REVIEW_APPROVED_STATUSES,
        "is_verified_purchase": bool(getattr(row, "is_verified_purchase", False)),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "reviewer_name": getattr(profile, "full_name", None) or "عميل",
        "reviewer_avatar_url": getattr(profile, "avatar_url", None),
    }


@router.get("/api/reviews/products/{product_id}")
async def api_public_product_reviews(
    product_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    review_model, columns, clauses = _public_product_review_filters(product_id)
    statement = select(review_model).where(*clauses)
    if "created_at" in columns:
        statement = statement.order_by(columns.created_at.desc())
    result = await session.execute(statement.limit(limit))
    return {"data": [await _product_review_response(session, row) for row in result.scalars()]}


@router.get("/api/reviews/products/{product_id}/stats")
async def api_public_product_review_stats(product_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    review_model, columns, clauses = _public_product_review_filters(product_id)
    if "rating" not in columns:
        return {"average": 0, "count": 0, "distribution": [0, 0, 0, 0, 0]}
    rating = columns.rating
    aggregate = await session.execute(
        select(func.avg(rating), func.count(rating)).select_from(review_model).where(*clauses)
    )
    average, count = aggregate.one()
    grouped = await session.execute(
        select(rating, func.count(rating)).select_from(review_model).where(*clauses).group_by(rating)
    )
    distribution = [0, 0, 0, 0, 0]
    for raw_rating, raw_count in grouped.all():
        try:
            index = int(raw_rating) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(distribution):
            distribution[index] = int(raw_count or 0)
    return {"average": round(float(average or 0), 2), "count": int(count or 0), "distribution": distribution}


@router.get("/api/reviews/products/{product_id}/eligibility")
async def api_product_review_eligibility(
    product_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    return await _review_eligibility(session, user=user, product_id=product_id)


@router.get("/api/reviews/products/{product_id}/mine")
async def api_my_product_review(
    product_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await _public_review_product(session, product_id)
    review_model, columns, _ = _public_product_review_filters(product_id)
    result = await session.execute(
        select(review_model)
        .where(
            columns.product_id == product_id,
            columns.user_id == user.id,
            columns.deleted_at.is_(None),
        )
        .order_by(columns.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return {"data": await _product_review_response(session, row) if row else None}


@router.post("/api/reviews/products/{product_id}", status_code=201)
async def api_create_product_review(
    product_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await _public_review_product(session, product_id)
    values = _review_input_values(await request.json(), request)
    eligibility = await _review_eligibility(session, user=user, product_id=product_id)
    if not eligibility["can_review"]:
        raise HTTPException(status_code=403, detail=eligibility["reason"])

    review_model = MODEL_BY_TABLE["product_reviews"]
    row = review_model(
        user_id=user.id,
        product_id=product_id,
        order_id=uuid.UUID(str(eligibility["order_id"])),
        status="pending",
        title="Product review",
        body=values["comment"],
        rating=values["rating"],
        comment=values["comment"],
        review_images=values["review_images"],
        is_verified_purchase=True,
        is_approved=False,
        extra_data={},
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    payload = await _product_review_response(session, row)
    await session.commit()
    return {"data": payload}


@router.patch("/api/reviews/{review_id}")
async def api_update_product_review(
    review_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    review_model = MODEL_BY_TABLE["product_reviews"]
    columns = review_model.__table__.c
    result = await session.execute(
        select(review_model).where(
            columns.id == review_id,
            columns.user_id == user.id,
            columns.deleted_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="review_not_found")
    values = _review_input_values(await request.json(), request)
    row.rating = values["rating"]
    row.comment = values["comment"]
    row.body = values["comment"]
    row.review_images = values["review_images"]
    row.status = "pending"
    row.is_approved = False
    await session.flush()
    await session.refresh(row)
    payload = await _product_review_response(session, row)
    await session.commit()
    return {"data": payload}


@router.delete("/api/reviews/{review_id}")
async def api_delete_product_review(
    review_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    review_model = MODEL_BY_TABLE["product_reviews"]
    columns = review_model.__table__.c
    result = await session.execute(
        select(review_model).where(
            columns.id == review_id,
            columns.user_id == user.id,
            columns.deleted_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="review_not_found")
    product_id = str(row.product_id)
    review_images = list(getattr(row, "review_images", None) or [])
    row.deleted_at = datetime.now(timezone.utc)
    row.status = "deleted"
    await session.commit()
    return {"data": {"deleted": True, "product_id": product_id, "review_images": review_images}}


@router.post("/api/reviews/store", status_code=201)
async def api_create_store_review(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    row = await _api_create(session, "store_reviews", {"user_id": user.id, "title": body.get("title") or "Review", "body": body.get("body") or body.get("comment") or "", "status": "pending", **body}, user)
    await session.commit()
    return {"data": row}


@router.patch("/api/reviews/store/{review_id}/status")
async def api_update_store_review_status(review_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = _normalize_store_review_status_payload(await request.json())
    row = await _api_update(session, "store_reviews", review_id, body, staff)
    await session.commit()
    return {"data": row}


@router.delete("/api/reviews/store/{review_id}")
async def api_delete_store_review(review_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("store_reviews", request, session, staff, review_id, "delete")


@router.post("/api/partnership/apply", status_code=201)
async def api_public_create_partner_application(
    request: Request,
    user: User | None = Depends(optional_user),
    session: AsyncSession = Depends(get_session),
):
    """Accept the public merchant application form.

    The management endpoint below remains staff-only.  Keeping this public
    submission route separate prevents a storefront customer from being
    evaluated by the staff authorization dependency.
    """
    body = await request.json()
    business_name = str(body.get("businessName") or body.get("name") or "").strip()
    email = str(body.get("email") or "").strip().lower()
    phone_digits = re.sub(r"\D", "", str(body.get("phone") or ""))
    phone = phone_digits[3:] if phone_digits.startswith("967") and len(phone_digits) == 12 else phone_digits
    business_type = str(body.get("businessType") or "").strip()
    description = str(body.get("description") or "").strip()

    if len(business_name) < 2:
        raise HTTPException(
            status_code=422,
            detail={"code": "partner_business_name_invalid", "message": "اسم النشاط التجاري يجب أن يكون حرفين على الأقل."},
        )
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(
            status_code=422,
            detail={"code": "partner_email_invalid", "message": "يرجى إدخال بريد إلكتروني صحيح."},
        )
    if not re.fullmatch(r"7\d{8}", phone):
        raise HTTPException(
            status_code=422,
            detail={"code": "partner_phone_invalid", "message": "رقم الهاتف اليمني يجب أن يكون 9 أرقام ويبدأ بالرقم 7."},
        )

    application_model = MODEL_BY_TABLE["partner_applications"]
    duplicate_matchers = [
        func.lower(application_model.email) == email,
        application_model.phone == phone,
    ]
    if user is not None:
        duplicate_matchers.append(application_model.user_id == user.id)
    duplicate_clauses = [
        application_model.deleted_at.is_(None),
        application_model.status.in_(["pending", "reviewing", "approved"]),
        or_(*duplicate_matchers),
    ]
    duplicate = (
        await session.execute(
            select(application_model)
            .where(*duplicate_clauses)
            .order_by(application_model.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        duplicate_email = str(getattr(duplicate, "email", "") or "").strip().lower() == email
        duplicate_phone = str(getattr(duplicate, "phone", "") or "").strip() == phone
        if user is not None and getattr(duplicate, "user_id", None) == user.id:
            code = "partner_application_exists"
            message = "لديك طلب تسجيل كتاجر قيد المراجعة أو تم اعتماده مسبقًا."
        elif duplicate_email:
            code = "partner_application_email_exists"
            message = "يوجد طلب مرتبط بهذا البريد الإلكتروني مسبقًا."
        elif duplicate_phone:
            code = "partner_application_phone_exists"
            message = "يوجد طلب مرتبط برقم الهاتف هذا مسبقًا."
        else:
            code = "partner_application_exists"
            message = "يوجد طلب تاجر مشابه قيد المراجعة مسبقًا."
        raise HTTPException(status_code=409, detail={"code": code, "message": message})

    application_user = user
    if application_user is None:
        existing_user = (
            await session.execute(select(User).where(func.lower(User.email) == email))
        ).scalar_one_or_none()
        if existing_user is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "partner_application_login_required",
                    "message": "هذا البريد مسجل مسبقًا. سجل الدخول ثم أرسل طلب الانضمام كتاجر.",
                },
            )
        application_user = await create_user(
            session,
            email=email,
            password=f"Partner-{uuid.uuid4()}-9A",
            full_name=business_name,
            phone=phone,
            city=body.get("city"),
            role="customer",
        )

    row = await _create_resource_row(
        session,
        "partner_applications",
        {
            "user_id": application_user.id,
            "name": business_name,
            "email": email,
            "phone": phone,
            "status": "pending",
            "description": description,
            "business_type": business_type or None,
        },
    )
    await session.commit()
    return {"data": row}


@router.post("/api/partnership/applications", status_code=201)
async def api_create_partner_application(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    user_id = body.get("user_id") or body.get("userId")
    if not user_id:
        user = await create_user(
            session,
            email=str(body.get("email") or f"partner-{uuid.uuid4()}@example.com"),
            password=f"Partner-{uuid.uuid4()}-9A",
            full_name=str(body.get("businessName") or body.get("name") or "Partner applicant"),
            phone=body.get("phone"),
            city=body.get("city"),
            role="customer",
        )
        user_id = user.id
    row = await _api_create(session, "partner_applications", {
        **body,
        "user_id": uuid.UUID(str(user_id)),
        "name": body.get("name") or body.get("businessName") or "Partner applicant",
        "status": body.get("status") or "pending",
    }, staff)
    await session.commit()
    return {"data": row}


@router.patch("/api/partnership/applications/{application_id}/status")
async def api_partner_application_status(application_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("partner_applications", request, session, staff, application_id, "update")


@router.patch("/api/partnership/applications/{application_id}/reject")
async def api_reject_partner_application(application_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE["partner_applications"]
    row = await session.get(model, application_id)
    if row is None:
        raise HTTPException(status_code=404, detail="application_not_found")
    row.status = "rejected"
    row.extra_data = {**(row.extra_data or {}), **_jsonable(body)}
    await session.commit()
    return {"data": serialize_record(row)}


@router.post("/api/partnership/applications/{application_id}/approve")
async def api_approve_partner_application(application_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    result = await execute_function(
        "approve_partner_application",
        {**body, "application_id": str(application_id)},
        staff,
        session,
        request,
    )
    await session.commit()
    application = result.get("application") or {}
    return {
        "data": {
            "applicationId": application.get("id"),
            "userId": application.get("user_id"),
            "application": application,
        }
    }


def _partner_rate(value: Any, default: int = 85) -> int | float:
    if value is None or str(value).strip() == "":
        return default
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="invalid_commission_rate")
    if not 0 <= rate <= 100:
        raise HTTPException(status_code=422, detail="invalid_commission_rate")
    return int(rate) if rate.is_integer() else rate


async def _ensure_partner_account(
    session: AsyncSession,
    body: dict[str, Any],
    staff: User,
    request: Request,
) -> tuple[User, Profile, Any, Any, bool]:
    raw_user_id = body.get("userId") or body.get("user_id") or body.get("partner_id")
    created = False
    if raw_user_id:
        try:
            user_id = uuid.UUID(str(raw_user_id))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid_partner_user")
        partner_user = await session.get(User, user_id, with_for_update=True)
        if partner_user is None or partner_user.deleted_at is not None:
            raise HTTPException(status_code=404, detail="partner_user_not_found")
    else:
        email = _first_text(body.get("email")).lower()
        password = str(body.get("password") or "")
        full_name = _first_text(body.get("fullName"), body.get("full_name"), body.get("name"))
        if not email:
            raise HTTPException(status_code=422, detail="partner_email_required")
        if not full_name:
            raise HTTPException(status_code=422, detail="partner_name_required")
        if not password:
            raise HTTPException(status_code=422, detail="partner_password_required")
        partner_user = await create_user(
            session,
            email=email,
            password=password,
            full_name=full_name,
            phone=_first_text(body.get("phone")) or None,
            city=_first_text(body.get("city")) or None,
            extra_data={
                key: value
                for key, value in {
                    "address": body.get("address"),
                    "governorate": body.get("governorate"),
                    "notes": body.get("notes"),
                }.items()
                if value not in (None, "")
            },
            role="customer",
        )
        created = True

    security = await account_security_for(session, partner_user.id, for_update=True)
    was_inactive = not bool(partner_user.is_active) or security.account_status != "active"
    partner_user.is_active = True
    security.account_status = "active"
    security.disabled_at = None
    if was_inactive:
        await bump_security_version(session, partner_user, reason="partner_account_enabled", request=request)

    role = await session.get(UserRole, {"user_id": partner_user.id, "role": "partner"})
    if role is None:
        session.add(UserRole(user_id=partner_user.id, role="partner"))

    profile_result = await session.execute(
        select(Profile).where(Profile.user_id == partner_user.id).with_for_update()
    )
    profile = profile_result.scalars().first()
    if profile is None:
        profile = Profile(
            id=partner_user.id,
            user_id=partner_user.id,
            email=partner_user.email,
            full_name=_first_text(body.get("fullName"), body.get("full_name"), body.get("name"), default="تاجر"),
            phone=_first_text(body.get("phone")) or None,
            city=_first_text(body.get("city")) or None,
            extra_data={},
        )
        session.add(profile)
    profile.deleted_at = None
    profile.email = profile.email or partner_user.email
    profile.full_name = profile.full_name or _first_text(body.get("fullName"), body.get("full_name"), body.get("name"), default="تاجر")
    profile.phone = profile.phone or _first_text(body.get("phone")) or None
    profile.city = profile.city or _first_text(body.get("city")) or None
    profile_extra = dict(profile.extra_data or {})
    for key in ("address", "governorate", "notes"):
        if body.get(key) not in (None, ""):
            profile_extra[key] = _jsonable(body[key])
    profile.extra_data = profile_extra

    storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    storefront_result = await session.execute(
        select(storefront_model)
        .where(or_(storefront_model.user_id == partner_user.id, storefront_model.partner_id == partner_user.id))
        .order_by(storefront_model.created_at.asc())
        .with_for_update()
    )
    storefront = storefront_result.scalars().first()
    if storefront is None:
        storefront = storefront_model(
            user_id=partner_user.id,
            partner_id=partner_user.id,
            name=_first_text(body.get("storeName"), body.get("store_name"), body.get("fullName"), body.get("name"), profile.full_name, default="متجر التاجر"),
            email=partner_user.email,
            phone=profile.phone,
            status="active",
            is_active=True,
        )
        session.add(storefront)
    else:
        storefront.user_id = storefront.user_id or partner_user.id
        storefront.partner_id = storefront.partner_id or partner_user.id
        storefront.name = storefront.name or _first_text(body.get("storeName"), body.get("store_name"), profile.full_name, default="متجر التاجر")
        storefront.email = storefront.email or partner_user.email
        storefront.phone = storefront.phone or profile.phone
        storefront.status = "active"
        storefront.is_active = True
        storefront.deleted_at = None

    contract_model = MODEL_BY_TABLE["partner_contracts"]
    contract_result = await session.execute(
        select(contract_model)
        .where(contract_model.partner_id == partner_user.id, contract_model.deleted_at.is_(None))
        .order_by(contract_model.created_at.asc())
        .with_for_update()
    )
    contract = contract_result.scalars().first()
    if contract is None:
        contract = contract_model(partner_id=partner_user.id, status="active", is_active=True, extra_data={})
        session.add(contract)
    rate = _partner_rate(body.get("commissionRate", body.get("commission_rate", body.get("rate"))))
    contract.status = "active"
    contract.is_active = True
    contract.extra_data = {
        **(contract.extra_data or {}),
        "rate": rate,
        "commissionRate": rate,
        **({"notes": _jsonable(body["notes"])} if body.get("notes") not in (None, "") else {}),
    }
    await session.flush()
    return partner_user, profile, storefront, contract, created


@router.post("/api/partnership/partners", status_code=201)
async def api_create_partner_contract(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="partner_payload_required")
    partner_user, profile, storefront, contract, created = await _ensure_partner_account(session, body, staff, request)
    await session.commit()
    return {
        "data": {
            "id": str(partner_user.id),
            "userId": str(partner_user.id),
            "partnerId": str(partner_user.id),
            "created": created,
            "name": profile.full_name or storefront.name,
            "email": partner_user.email,
            "storefront": serialize_record(storefront),
            "contract": serialize_record(contract),
        }
    }


@router.patch("/api/partnership/partners/{partner_id}/commission")
async def api_update_partner_commission(partner_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["partner_contracts"]
    rows = await _rows(session, "partner_contracts", clauses=(model.partner_id == partner_id,), limit=1)
    row = rows[0] if rows else model(partner_id=partner_id, status="active", is_active=True, extra_data={})
    if not rows:
        session.add(row)
    body = await request.json()
    row.extra_data = {**(row.extra_data or {}), **_jsonable(body)}
    await session.commit()
    return {"data": serialize_record(row)}


@router.delete("/api/partnership/partners/{partner_id}")
async def api_delete_partner_contract(
    partner_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    """Remove a merchant from active listings without leaving a ghost row.

    The old handler deleted only the contract, while the admin list is backed
    by partner_storefronts.  Soft-deleting both records makes the result
    durable after a refresh and keeps an auditable history.  Product removal
    is opt-in and uses the same image-asset cleanup as the normal product
    deletion endpoint.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="partner_delete_payload_required")
    delete_products = body.get("deactivateProducts", body.get("deactivate_products", False))
    if not isinstance(delete_products, bool):
        raise HTTPException(status_code=422, detail="deactivate_products_must_be_boolean")

    now = datetime.now(timezone.utc)
    removed_storefronts = 0
    removed_contracts = 0
    storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    storefront_rows = await _rows(
        session,
        "partner_storefronts",
        clauses=(or_(storefront_model.partner_id == partner_id, storefront_model.user_id == partner_id),),
        limit=50,
    )
    for storefront in storefront_rows:
        storefront.deleted_at = now
        storefront.is_active = False
        storefront.status = "inactive"
        removed_storefronts += 1

    contract_model = MODEL_BY_TABLE["partner_contracts"]
    contract_rows = await _rows(session, "partner_contracts", clauses=(contract_model.partner_id == partner_id,), limit=50)
    for contract in contract_rows:
        contract.deleted_at = now
        contract.is_active = False
        contract.status = "inactive"
        removed_contracts += 1

    local_merchant_model = MODEL_BY_TABLE.get("local_merchants")
    removed_local_merchants = 0
    if local_merchant_model is not None:
        local_rows = await _rows(session, "local_merchants", clauses=(local_merchant_model.user_id == partner_id,), limit=50)
        for merchant in local_rows:
            merchant.deleted_at = now
            merchant.is_active = False
            merchant.status = "inactive"
            removed_local_merchants += 1

    removed_products = 0
    removed_variants = 0
    removed_assets = 0
    if delete_products:
        product_rows = list(
            (
                await session.execute(
                    select(Product)
                    .where(Product.partner_id == partner_id, Product.deleted_at.is_(None))
                    .with_for_update()
                )
            ).scalars()
        )
        for product in product_rows:
            variants = list(
                (
                    await session.execute(
                        select(ProductVariant)
                        .where(ProductVariant.product_id == product.id, ProductVariant.deleted_at.is_(None))
                        .with_for_update()
                    )
                ).scalars()
            )
            removed_assets += await _delete_product_file_assets(
                session,
                product=product,
                variants=variants,
                actor=staff,
            )
            product.is_active = False
            product.is_featured = False
            product.approval_status = "inactive"
            product.deleted_at = now
            removed_products += 1
            for variant in variants:
                variant.is_active = False
                variant.deleted_at = now
                removed_variants += 1

    await session.commit()
    return {
        "ok": True,
        "removed_storefronts": removed_storefronts,
        "removed_contracts": removed_contracts,
        "removed_local_merchants": removed_local_merchants,
        "removed_products": removed_products,
        "removed_variants": removed_variants,
        "removed_assets": removed_assets,
    }


@router.post("/api/marketing/marketers", status_code=201)
async def api_create_marketer(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = _normalize_marketer_body(await request.json(), for_create=True)
    if await session.get(User, body["user_id"]) is None:
        raise HTTPException(status_code=404, detail="marketer_user_not_found")
    row = await _api_create(session, "marketers", body, staff)
    await session.commit()
    return {"data": _marketer_payload(row)}


@router.patch("/api/marketing/marketers/{marketer_id}")
async def api_update_marketer(marketer_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = _normalize_marketer_body(await request.json())
    if "user_id" in body and await session.get(User, body["user_id"]) is None:
        raise HTTPException(status_code=404, detail="marketer_user_not_found")
    row = await _api_update(session, "marketers", marketer_id, body, staff)
    await session.commit()
    return {"data": _marketer_payload(row)}


@router.delete("/api/marketing/marketers/{marketer_id}")
async def api_delete_marketer(marketer_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("marketers", request, session, staff, marketer_id, "delete")


@router.post("/api/marketing/commissions/pay")
async def api_pay_marketer_commission(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    raw_ids = body.get("ids") or body.get("commission_ids") or body.get("commissionIds")
    if not raw_ids:
        raw_id = body.get("commission_id") or body.get("commissionId")
        raw_ids = [raw_id] if raw_id else []
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(status_code=422, detail="commission_ids_required")
    commission_ids = [_uuid(value, "commission_id") for value in raw_ids]
    model = MODEL_BY_TABLE["marketer_commissions"]
    commissions = list(
        (
            await session.execute(
                select(model)
                .where(model.id.in_(commission_ids), model.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalars()
    )
    if not commissions:
        raise HTTPException(status_code=404, detail="commission_not_found")
    if len(commissions) != len(set(commission_ids)):
        raise HTTPException(status_code=404, detail="commission_not_found")
    payable_statuses = {"pending", "earned", "approved", "unpaid"}
    if any(str(commission.status or "").lower() not in payable_statuses for commission in commissions):
        raise HTTPException(status_code=409, detail="commission_already_paid")
    grouped: dict[uuid.UUID, list[Any]] = {}
    for commission in commissions:
        commission.status = "paid"
        grouped.setdefault(commission.user_id, []).append(commission)
    rows = []
    for user_id, user_commissions in grouped.items():
        rows.append(
            await _api_create(
                session,
                "marketer_payments",
                {
                    "user_id": user_id,
                    "amount": sum((money(item.amount or 0) for item in user_commissions), money(0)),
                    "status": "paid",
                    "notes": body.get("notes") or "",
                    "commissions_paid": [str(item.id) for item in user_commissions],
                },
                staff,
            )
        )
    await session.commit()
    return {"data": rows, "count": len(commissions)}


@router.post("/api/operations/refunds", status_code=201)
async def api_create_refund(
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    order_id = _uuid(body.get("order_id") or body.get("orderId"), "order_id")
    key = str(idempotency_key or body.get("idempotencyKey") or body.get("idempotency_key") or "").strip()
    if not key:
        key = f"operations-refund-{uuid.uuid4().hex}"
    endpoint = "/api/operations/refunds"
    digest = request_hash(body)
    await advisory_xact_lock(session, f"idempotency:{endpoint}:{key}")
    existing = await find_idempotent_refund(
        session,
        actor_id=user.id,
        endpoint=endpoint,
        key=key,
        request_digest=digest,
    )
    if existing is not None:
        response.status_code = 200
        payload = financial_response_row(existing)
        payload["idempotency_replayed"] = True
        return {"data": payload}
    row = await create_refund_request(
        session,
        order_id=order_id,
        body=body,
        staff=user,
        roles=roles,
        idempotency_key=key,
        request_digest=digest,
        endpoint=endpoint,
    )
    return {"data": row}


@router.patch("/api/operations/refunds/{refund_id}/status")
async def api_update_refund_status(
    refund_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    row = await update_refund_workflow_status(
        session,
        refund_id=refund_id,
        body=await request.json(),
        staff=staff,
        roles=roles,
    )
    return {"data": row}


@router.patch("/api/operations/shipping/orders/{order_id}")
async def api_assign_order_shipping(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE["order_shipping"]
    rows = await _rows(session, "order_shipping", clauses=(model.order_id == order_id,), limit=1)
    row = rows[0] if rows else model(order_id=order_id, status="assigned", fee=0, description="", extra_data={})
    if not rows:
        session.add(row)
        await session.flush()
    row.status = "assigned"
    row.extra_data = {**(row.extra_data or {}), "carrier_id": str(body.get("carrierId") or body.get("carrier_id") or ""), "tracking_number": body.get("trackingNumber") or body.get("tracking_number")}
    await session.commit()
    return {"data": serialize_record(row)}


@router.post("/api/operations/shipping/orders/{order_id}/stage", status_code=201)
async def api_add_order_shipping_stage(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    row = await _api_create(session, "shipping_history", {"order_id": order_id, "status": "stage", "notes": body.get("notes") or "", "stage_id": str(body.get("stageId") or body.get("stage_id") or ""), "location": body.get("location")}, staff)
    await session.commit()
    return {"data": row}


@router.get("/api/operations/shipping/shipments/{shipment_id}/history")
async def api_order_shipping_history(shipment_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["shipping_history"]
    rows = await _rows(session, "shipping_history", clauses=(model.extra_data["order_shipping_id"].astext == str(shipment_id),), limit=100)
    if not rows:
        shipping = await session.get(MODEL_BY_TABLE["order_shipping"], shipment_id)
        if shipping is not None:
            rows = await _rows(session, "shipping_history", clauses=(model.order_id == shipping.order_id,), limit=100)
    return {"data": [serialize_record(row) for row in rows]}


@router.post("/api/operations/operational/days/open")
@router.post("/api/operations/operational/days/validate")
@router.post("/api/operations/operational/days/close")
async def api_operational_day_action(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    action = request.url.path.rsplit("/", 1)[-1]
    return await OperationalDayService().action(session, actor=staff, action=action, raw_date=body.get("date"))


@router.patch("/api/operations/operational/alerts/{alert_id}/resolve")
async def api_resolve_operational_alert(alert_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("operational_alerts", request, session, staff, alert_id, "update")


def _international_purchase_link_id(row: Any) -> str | None:
    extra = dict(getattr(row, "extra_data", None) or {}) if not isinstance(row, dict) else row
    value = _first_text(extra.get("purchase_id"), extra.get("purchaseId"))
    return value or None


async def _international_order_payloads(session: AsyncSession, rows: list[Any]) -> list[dict[str, Any]]:
    payloads = [serialize_record(row) for row in rows]
    user_ids = {row.user_id for row in rows if getattr(row, "user_id", None) is not None}
    profiles_by_user: dict[str, dict[str, Any]] = {}
    if user_ids:
        profile_result = await session.execute(
            select(Profile).where(Profile.user_id.in_(user_ids), Profile.deleted_at.is_(None))
        )
        profiles_by_user = {
            str(profile.user_id): serialize_record(profile)
            for profile in profile_result.scalars()
        }
    for row, payload in zip(rows, payloads):
        profile = profiles_by_user.get(str(row.user_id)) if getattr(row, "user_id", None) else None
        payload["profiles"] = profile
        linked_purchase_id = _international_purchase_link_id(row)
        if linked_purchase_id:
            payload["purchase_id"] = linked_purchase_id
            payload["purchaseId"] = linked_purchase_id
    return payloads


@router.post("/api/admin-shopping/purchases", status_code=201)
async def api_create_international_purchase(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="purchase_payload_required")
    source_site = _first_text(body.get("source_site"), body.get("sourceSite"))
    if not source_site:
        raise HTTPException(status_code=422, detail="source_site_required")
    purchase_number = _first_text(
        body.get("purchase_number"),
        body.get("purchaseNumber"),
        default=f"PUR-{uuid.uuid4().hex[:8].upper()}",
    )
    payload = {
        **body,
        "user_id": staff.id,
        "status": body.get("status") or "draft",
        "amount": body.get("amount") or 0,
        "description": body.get("notes") or body.get("source_url") or source_site,
        "purchase_number": purchase_number,
        "source_site": source_site,
    }
    row = await _api_create(session, "international_purchases", payload, staff)
    await session.commit()
    return {"data": row}


@router.patch("/api/admin-shopping/purchases/{purchase_id}/status")
async def api_update_international_purchase_status(purchase_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("international_purchases", request, session, staff, purchase_id, "update")


@router.get("/api/admin-shopping/international-orders/{order_id}/context")
async def api_international_order_context(order_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await session.get(MODEL_BY_TABLE["international_orders"], order_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="international_order_not_found")
    profile_result = await session.execute(
        select(Profile)
        .where(Profile.user_id == row.user_id, Profile.deleted_at.is_(None))
        .limit(1)
    )
    payload = serialize_record(row)
    linked_purchase_id = _international_purchase_link_id(row)
    if linked_purchase_id:
        payload["purchase_id"] = linked_purchase_id
        payload["purchaseId"] = linked_purchase_id
    profile = profile_result.scalar_one_or_none()
    payload["customerProfile"] = serialize_record(profile) if profile is not None else None
    purchases = await _rows(session, "international_purchases", limit=500)
    purchase_payloads = [serialize_record(purchase) for purchase in purchases]
    payload["availablePurchases"] = purchase_payloads
    payload["linkedPurchase"] = next(
        (purchase for purchase in purchase_payloads if str(purchase.get("id")) == linked_purchase_id),
        None,
    )
    return {"data": payload}


@router.patch("/api/admin-shopping/international-orders/{order_id}/status")
@router.patch("/api/admin-shopping/international-orders/{order_id}/assignment")
async def api_patch_international_order(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("international_orders", request, session, staff, order_id, "update")


@router.post("/api/admin-shopping/international-orders/{order_id}/pricing")
async def api_price_international_order(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="pricing_payload_required")

    model = MODEL_BY_TABLE["international_orders"]
    row = await session.get(model, order_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="international_order_not_found")
    if row.user_id is None:
        raise HTTPException(status_code=422, detail="international_order_customer_required")

    raw_item_prices = body.get("itemPrices")
    if raw_item_prices is None:
        raw_item_prices = body.get("item_prices")
    if not isinstance(raw_item_prices, list) or not raw_item_prices:
        raise HTTPException(status_code=422, detail="item_prices_required")

    normalized_item_prices: list[dict[str, Any]] = []
    items_total = Decimal("0")
    for raw_item in raw_item_prices:
        if not isinstance(raw_item, dict):
            raise HTTPException(status_code=422, detail="invalid_item_price")
        price = _money_from_payload(raw_item.get("price", raw_item.get("unit_price", raw_item.get("final_price"))))
        try:
            quantity = int(raw_item.get("quantity") or 1)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid_quantity")
        if quantity < 1 or quantity > 1000:
            raise HTTPException(status_code=422, detail="invalid_quantity")
        normalized_item_prices.append({"price": format(price, "f"), "quantity": quantity})
        items_total += price * quantity

    shipping_cost = _money_from_payload(body.get("shippingCost", body.get("shipping_cost", 0)))
    service_fee = _money_from_payload(body.get("serviceFee", body.get("service_fee", 0)))
    customs_cost = _money_from_payload(body.get("customsCost", body.get("customs_cost", 0)))
    grand_total = items_total + shipping_cost + service_fee + customs_cost
    if grand_total <= 0:
        raise HTTPException(status_code=422, detail="pricing_total_required")

    currency_code = _first_text(
        body.get("currencyCode"),
        body.get("currency_code"),
        (row.extra_data or {}).get("currency_code"),
        default="YER",
    ).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{2,8}", currency_code):
        raise HTTPException(status_code=422, detail="invalid_currency_code")

    extra = dict(row.extra_data or {})
    existing_items = extra.get("items")
    updated_items = existing_items
    if isinstance(existing_items, list):
        updated_items = []
        for index, existing_item in enumerate(existing_items):
            if not isinstance(existing_item, dict):
                updated_items.append(existing_item)
                continue
            updated_item = dict(existing_item)
            if index < len(normalized_item_prices):
                item_price = Decimal(normalized_item_prices[index]["price"])
                item_quantity = normalized_item_prices[index]["quantity"]
                item_total = item_price * item_quantity
                updated_item.update(
                    {
                        "quantity": item_quantity,
                        "unit_price": format(item_price, "f"),
                        "final_price": format(item_price, "f"),
                        "subtotal": format(item_total, "f"),
                        "line_total": format(item_total, "f"),
                    }
                )
            updated_items.append(updated_item)

    pricing_updated_at = datetime.now(timezone.utc)
    current_status = str(row.status or "").lower()
    if current_status not in {"confirmed", "processing", "processed", "shipping", "shipped", "delivering", "delivered", "cancelled"}:
        row.status = "reviewing"
    row.amount = grand_total
    extra.update(
        {
            "items": _jsonable(updated_items),
            "item_prices": _jsonable(normalized_item_prices),
            "itemPrices": _jsonable(normalized_item_prices),
            "shipping_cost": format(shipping_cost, "f"),
            "shippingCost": format(shipping_cost, "f"),
            "service_fee": format(service_fee, "f"),
            "serviceFee": format(service_fee, "f"),
            "customs_cost": format(customs_cost, "f"),
            "customsCost": format(customs_cost, "f"),
            "currency_code": currency_code,
            "currencyCode": currency_code,
            "final_cost": format(grand_total, "f"),
            "pricing_updated_at": pricing_updated_at.isoformat(),
            "pricing_updated_by": str(staff.id),
        }
    )
    row.extra_data = extra

    custom_message = _first_text(body.get("message"), body.get("notificationMessage"), body.get("notification_message"))
    notification_message = custom_message or (
        f"تم تحديث تسعير طلب الشراء الدولي إلى {format(grand_total, 'f')} {currency_code}. "
        "يرجى مراجعة الطلب وتأكيده للمتابعة."
    )
    if custom_message and f"{format(grand_total, 'f')} {currency_code}" not in custom_message:
        notification_message = f"{custom_message}\n\nالإجمالي الجديد: {format(grand_total, 'f')} {currency_code}"

    notification_created = False
    if row.user_id:
        await NotificationService(session).create_notification(
            NotificationPayload(
                user_id=row.user_id,
                title="تم تحديث تسعير طلبك الدولي",
                body=notification_message,
                notification_type="order_update",
                category="order",
                priority="high",
                action_type="open_order",
                action_url=f"/my-orders?highlight={order_id}",
                entity_type="international_orders",
                entity_id=str(order_id),
                order_id=order_id,
                payload={
                    "orderId": str(order_id),
                    "status": row.status,
                    "finalCost": float(grand_total),
                    "currencyCode": currency_code,
                },
                created_by=staff.id,
                deduplication_key=f"international-order-pricing:{order_id}:{pricing_updated_at.isoformat()}",
            )
        )
        notification_created = True

    event_payload = {
        "orderId": str(order_id),
        "status": row.status,
        "finalCost": float(grand_total),
        "estimatedCost": float(grand_total),
        "currencyCode": currency_code,
    }
    await _record_and_publish_realtime(
        session,
        channel=f"user:{row.user_id}",
        event="international-order.status_changed",
        payload=event_payload,
        dedupe_key=f"international-order.status_changed:{order_id}:{pricing_updated_at.isoformat()}",
        user_id=row.user_id,
    )

    notify_by_whatsapp = bool(body.get("notifyByWhatsApp", body.get("notify_by_whatsapp", False)))
    whatsapp_queued = False
    whatsapp_phone = _first_text(body.get("customerPhone"), body.get("customer_phone"), (extra.get("phone")))
    if notify_by_whatsapp:
        profile_result = await session.execute(
            select(Profile)
            .where(Profile.user_id == row.user_id, Profile.deleted_at.is_(None))
            .limit(1)
        )
        profile = profile_result.scalar_one_or_none()
        whatsapp_phone = _first_text(whatsapp_phone, getattr(profile, "phone", None))
        if whatsapp_phone:
            whatsapp_model = MODEL_BY_TABLE["whatsapp_outbox"]
            session.add(
                whatsapp_model(
                    user_id=row.user_id,
                    title="تحديث تسعير طلبك الدولي",
                    status="queued",
                    phone=whatsapp_phone,
                    message=notification_message,
                    extra_data={
                        "order_id": str(order_id),
                        "category": "order",
                        "template": "international_order_pricing",
                        "pricing_updated_at": pricing_updated_at.isoformat(),
                    },
                )
            )
            whatsapp_queued = True

    await session.commit()
    return {
        "data": serialize_record(row),
        "notification": {
            "in_app": notification_created,
            "whatsapp_queued": whatsapp_queued,
            "whatsapp_phone_available": bool(whatsapp_phone),
        },
    }


@router.post("/api/admin-shopping/international-orders/{order_id}/payment-status")
async def api_international_order_payment_status(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE["international_orders"]
    row = await session.get(model, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="international_order_not_found")
    extra = dict(row.extra_data or {})
    extra["payment_status"] = body.get("status") or "paid"
    row.extra_data = extra
    await session.commit()
    return {"data": serialize_record(row)}


@router.post("/api/admin-shopping/international-orders/{order_id}/link-purchase")
async def api_link_international_purchase(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="purchase_link_payload_required")
    purchase_id = _uuid(body.get("purchaseId") or body.get("purchase_id"), "purchaseId")
    purchase_model = MODEL_BY_TABLE["international_purchases"]
    purchase = await session.get(purchase_model, purchase_id)
    if purchase is None or purchase.deleted_at is not None:
        raise HTTPException(status_code=404, detail="international_purchase_not_found")

    model = MODEL_BY_TABLE["international_orders"]
    row = await session.get(model, order_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="international_order_not_found")
    current_purchase_id = _international_purchase_link_id(row)
    if current_purchase_id and current_purchase_id != str(purchase_id):
        raise HTTPException(status_code=409, detail="ORDER_ALREADY_LINKED_TO_PURCHASE")
    extra = dict(row.extra_data or {})
    extra["purchase_id"] = str(purchase_id)
    extra["purchaseId"] = str(purchase_id)
    row.extra_data = extra
    await session.commit()
    payload = serialize_record(row)
    payload["purchase_id"] = str(purchase_id)
    payload["purchaseId"] = str(purchase_id)
    return {"data": payload}


@router.delete("/api/admin-shopping/international-orders/{order_id}/link-purchase")
async def api_unlink_international_purchase(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["international_orders"]
    row = await session.get(model, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="international_order_not_found")
    extra = dict(row.extra_data or {})
    extra.pop("purchaseId", None)
    extra.pop("purchase_id", None)
    row.extra_data = extra
    await session.commit()
    return {"data": serialize_record(row)}


@router.post("/api/admin-shopping/order-links")
async def api_link_local_international_order(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="order_link_payload_required")
    order_id = _uuid(body.get("localOrderId") or body.get("local_order_id"), "localOrderId")
    intl_uuid = _uuid(body.get("internationalOrderId") or body.get("international_order_id"), "internationalOrderId")
    intl_id = str(intl_uuid)
    model = MODEL_BY_TABLE["orders"]
    row = await session.get(model, order_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="order_not_found")
    international_model = MODEL_BY_TABLE["international_orders"]
    international_order = await session.get(international_model, intl_uuid)
    if international_order is None or international_order.deleted_at is not None:
        raise HTTPException(status_code=404, detail="international_order_not_found")

    local_extra = dict(row.extra_data or {})
    existing_local_link = _order_link_value(row, ORDER_LINK_INTERNATIONAL_KEYS)
    if existing_local_link and existing_local_link != intl_id:
        raise HTTPException(status_code=409, detail="LOCAL_ALREADY_LINKED")
    existing_intl_link = _order_link_value(international_order, ORDER_LINK_LOCAL_KEYS)
    if existing_intl_link and existing_intl_link != str(order_id):
        raise HTTPException(status_code=409, detail="INTERNATIONAL_ALREADY_LINKED")
    for linked_row in await _rows(session, "orders", limit=1000):
        if linked_row.id == row.id:
            continue
        linked_id = _order_link_value(linked_row, ORDER_LINK_INTERNATIONAL_KEYS)
        if linked_id == intl_id:
            raise HTTPException(status_code=409, detail="INTERNATIONAL_ALREADY_LINKED")

    row.extra_data = {
        **local_extra,
        "order_linking_candidate": True,
        "linked_international_order_id": intl_id,
        "linkedInternationalOrderId": intl_id,
    }
    international_extra = dict(international_order.extra_data or {})
    international_order.extra_data = {
        **international_extra,
        "linked_local_order_id": str(order_id),
        "linkedLocalOrderId": str(order_id),
    }
    await session.commit()
    local_payload = serialize_record(row)
    international_payload = serialize_record(international_order)
    local_payload["linked_international_order_id"] = intl_id
    international_payload["linked_local_order_id"] = str(order_id)
    return {"data": {"local_order": local_payload, "international_order": international_payload}}


@router.delete("/api/admin-shopping/order-links/{order_id}")
async def api_unlink_local_international_order(order_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["orders"]
    row = await session.get(model, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    extra = dict(row.extra_data or {})
    linked_international_id = _order_link_value(row, ORDER_LINK_INTERNATIONAL_KEYS)
    extra.pop("linked_international_order_id", None)
    extra.pop("linkedInternationalOrderId", None)
    row.extra_data = extra
    international_rows = await _rows(session, "international_orders", limit=1000)
    for international_order in international_rows:
        reverse_local_id = _order_link_value(international_order, ORDER_LINK_LOCAL_KEYS)
        if (linked_international_id and str(international_order.id) == linked_international_id) or reverse_local_id == str(order_id):
            international_extra = dict(international_order.extra_data or {})
            international_extra.pop("linked_local_order_id", None)
            international_extra.pop("linkedLocalOrderId", None)
            international_order.extra_data = international_extra
    await session.commit()
    return {"data": {"local_order": serialize_record(row)}}


@router.patch("/api/admin-shopping/local-requests/{request_id}")
async def api_patch_local_shopping_request(request_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("local_shopping_requests", request, session, staff, request_id, "update")


@router.get("/api/payments/local/{request_id}")
async def api_list_local_payments(
    request_id: uuid.UUID,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    model = MODEL_BY_TABLE["order_payments"]
    result = await session.execute(
        select(model)
        .where(
            model.deleted_at.is_(None),
            model.extra_data["local_request_id"].astext == str(request_id),
        )
        .order_by(model.created_at.desc())
    )
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.post("/api/payments/local/{request_id}", status_code=201)
async def api_create_local_payment(
    request_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    local_request = await session.get(MODEL_BY_TABLE["local_shopping_requests"], request_id)
    if local_request is None:
        raise HTTPException(status_code=404, detail="local_request_not_found")
    body = await request.json()
    _validate_payment_record_body(body)
    payload = {
        **body,
        "order_id": None,
        "type": body.get("payment_method") or "local",
        "amount": body.get("amount") or 0,
        "status": body.get("status") or "pending",
        "local_request_id": str(request_id),
    }
    row = await _api_create(session, "order_payments", payload, staff)
    await session.commit()
    return {"data": row}


@router.post("/api/payments/local/{request_id}/receipt", status_code=201)
async def api_upload_local_payment_receipt(
    request_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    local_request = await session.get(MODEL_BY_TABLE["local_shopping_requests"], request_id)
    if local_request is None:
        raise HTTPException(status_code=404, detail="local_request_not_found")
    return {
        "data": await _secure_upload_from_request(
            request,
            user=user,
            roles=roles,
            session=session,
            forced_policy="payment_receipt",
            entity_type="local_payment_request",
            entity_id=request_id,
        )
    }


@router.patch("/api/payments/local/records/{payment_id}")
async def api_update_local_payment(
    payment_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    body = await request.json()
    _validate_payment_record_body(body)
    row = await _api_update(session, "order_payments", payment_id, body, staff)
    await session.commit()
    return {"data": row}


@router.delete("/api/payments/local/records/{payment_id}")
async def api_delete_local_payment(
    payment_id: uuid.UUID,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    await _api_delete(session, "order_payments", payment_id)
    await session.commit()
    return {"ok": True}


@router.post("/api/finance/marketer-payments", status_code=201)
async def api_create_marketer_payment(request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    require_finance_actor(roles)
    body = await request.json()
    _validate_payment_record_body(body)
    user_id: uuid.UUID | None = None
    raw_marketer_id = body.get("marketer_id") or body.get("marketerId")
    if raw_marketer_id:
        marketer_model = MODEL_BY_TABLE["marketers"]
        marketer = await session.get(marketer_model, _uuid(raw_marketer_id, "marketer_id"))
        if marketer is not None:
            user_id = marketer.user_id
    if user_id is None:
        raw_user_id = body.get("user_id") or body.get("userId")
        if raw_user_id:
            user_id = _uuid(raw_user_id, "user_id")
    if user_id is None:
        raise HTTPException(status_code=422, detail="marketer_user_required")
    if await session.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="marketer_user_not_found")
    row = await _api_create(
        session,
        "marketer_payments",
        {"user_id": user_id, "amount": body.get("amount") or 0, "status": body.get("status") or "pending", "notes": body.get("notes") or "", **body},
        staff,
    )
    await session.commit()
    return {"data": row}


@router.post("/api/finance/partner-payments", status_code=201)
async def api_create_partner_payment(request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    require_finance_actor(roles)
    body = await request.json()
    _validate_payment_record_body(body)
    partner_id = uuid.UUID(str(body.get("partner_id") or body.get("partnerId")))
    row = await _api_create(session, "partner_payments", {"partner_id": partner_id, "amount": body.get("amount") or 0, "status": body.get("status") or "pending", **body}, staff)
    settlement_model = MODEL_BY_TABLE["partner_settlements"]
    for settlement_id in body.get("orders_settled", []) or []:
        settlement = await session.get(settlement_model, uuid.UUID(str(settlement_id)))
        if settlement is not None:
            settlement.status = body.get("status") or "paid"
    await session.commit()
    return {"data": row}


@router.post("/api/finance/international-orders/{order_id}/payments", status_code=201)
async def api_create_international_payment(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    require_finance_actor(roles)
    international_order = await session.get(MODEL_BY_TABLE["international_orders"], order_id)
    if international_order is None:
        raise HTTPException(status_code=404, detail="international_order_not_found")
    body = await request.json()
    _validate_payment_record_body(body)
    payload = {
        **body,
        "order_id": order_id,
        "amount": body.get("amount") or 0,
        "status": body.get("status") or "pending",
    }
    row = await _api_create(session, "international_order_payments", payload, staff)
    await session.commit()
    return {"data": row}


@router.get("/api/finance/international-orders/{order_id}/payments")
async def api_list_international_payments(
    order_id: uuid.UUID,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    model = MODEL_BY_TABLE["international_order_payments"]
    result = await session.execute(
        select(model)
        .where(model.deleted_at.is_(None), model.order_id == order_id)
        .order_by(model.created_at.desc())
    )
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.post("/api/finance/international-orders/{order_id}/receipt", status_code=201)
async def api_upload_international_payment_receipt(
    order_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    international_order = await session.get(MODEL_BY_TABLE["international_orders"], order_id)
    if international_order is None:
        raise HTTPException(status_code=404, detail="international_order_not_found")
    return {
        "data": await _secure_upload_from_request(
            request,
            user=user,
            roles=roles,
            session=session,
            forced_policy="payment_receipt",
            entity_type="international_payment_order",
            entity_id=order_id,
        )
    }


@router.patch("/api/finance/international-order-payments/{payment_id}")
async def api_update_international_payment(payment_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    require_finance_actor(roles)
    body = await request.json()
    _validate_payment_record_body(body)
    row = await _api_update(session, "international_order_payments", payment_id, body, staff)
    await session.commit()
    return {"data": row}


@router.delete("/api/finance/international-order-payments/{payment_id}")
async def api_delete_international_payment(payment_id: uuid.UUID, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    require_finance_actor(roles)
    await _api_delete(session, "international_order_payments", payment_id)
    await session.commit()
    return {"ok": True}


@router.post("/api/finance/international-orders/{order_id}/expenses", status_code=201)
async def api_create_international_expense(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    row = await _api_create(session, "general_expenses", {"amount": body.get("amount") or 0, "type": body.get("category") or "international", "description": body.get("description") or "", "status": "pending", "international_order_id": str(order_id), **body}, staff)
    await session.commit()
    return {"data": _general_expense_payload(row)}


@router.patch("/api/finance/international-order-expenses/{expense_id}")
async def api_update_international_expense(expense_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("general_expenses", request, session, staff, expense_id, "update")


@router.delete("/api/finance/international-order-expenses/{expense_id}")
async def api_delete_international_expense(expense_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("general_expenses", request, session, staff, expense_id, "delete")


@router.post("/api/admin-data/import/products")
async def api_import_products(request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.import")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail={"code": "import_payload_required", "message": "بيانات الاستيراد مطلوبة."})
    products = body.get("products") or body.get("data") or []
    if not isinstance(products, list) or not products:
        raise HTTPException(status_code=422, detail={"code": "import_products_required", "message": "الملف يجب أن يحتوي على قائمة منتجات."})
    product_model = MODEL_BY_TABLE["products"]
    success = 0
    failed = 0
    errors: list[str] = []
    for index, item in enumerate(products[:5000], start=1):
        savepoint = await session.begin_nested()
        try:
            if not isinstance(item, dict):
                raise HTTPException(status_code=422, detail="بيانات المنتج غير صحيحة")
            values = _normalize_catalog_mutation_input(dict(item))
            for field in ("id", "short_code", "created_at", "updated_at", "deleted_at", "approved_by", "approved_at"):
                values.pop(field, None)
            for field in ("is_active", "is_featured", "track_inventory"):
                if isinstance(values.get(field), str):
                    values[field] = values[field].strip().lower() not in {"false", "0", "no", "inactive"}
            for field in ("price", "original_price"):
                if values.get(field) == "":
                    values[field] = "0" if field == "price" else None
            for field in ("stock_quantity", "min_stock_quantity"):
                if values.get(field) == "":
                    values.pop(field, None)
            for field in ("images", "tags"):
                if isinstance(values.get(field), str):
                    raw = values[field].strip()
                    if not raw:
                        values[field] = []
                    else:
                        try:
                            parsed = json.loads(raw)
                        except json.JSONDecodeError:
                            parsed = [raw]
                        values[field] = parsed if isinstance(parsed, list) else [parsed]
            for field in ("category_id", "brand_id", "supplier_id", "partner_id"):
                if values.get(field) in (None, ""):
                    values[field] = None
                else:
                    values[field] = _uuid(values[field], field)
            values.setdefault("approval_status", "approved")
            values = normalize_product_mutation_values(values, partial=False)
            values.setdefault("is_active", True)
            if str(values.get("approval_status") or "").lower() in {"approved", "active", "published"}:
                values.setdefault("approved_by", staff.id)
                values.setdefault("approved_at", datetime.now(timezone.utc))
            name = str(values["name"]).strip()
            duplicate_clauses = [func.lower(product_model.name) == name.lower()]
            if values.get("sku"):
                duplicate_clauses.append(product_model.sku == values["sku"])
            existing = await _rows(
                session,
                "products",
                clauses=(or_(*duplicate_clauses),),
                limit=1,
            )
            if existing:
                raise HTTPException(status_code=409, detail="المنتج موجود مسبقاً بالاسم أو الرمز.")
            await _create_resource_row(session, "products", values)
            await savepoint.commit()
            success += 1
        except Exception as exc:
            await savepoint.rollback()
            failed += 1
            if isinstance(exc, HTTPException):
                detail = exc.detail
                if isinstance(detail, dict):
                    message = str(detail.get("message") or detail.get("code") or "بيانات غير صحيحة")
                else:
                    message = str(detail)
            else:
                message = "تعذر حفظ المنتج؛ تحقق من المراجع والقيم المكررة."
            errors.append(f"السطر {index}: {message}")
    await session.commit()
    return JSONResponse(
        status_code=200,
        content={"data": {"success": success, "failed": failed, "errors": errors[:200], "truncated": len(products) > 5000}},
    )


@router.post("/api/analytics/events", status_code=201)
async def api_create_analytics_event(request: Request, user: User | None = Depends(optional_user), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    events = body if isinstance(body, list) else [body]
    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type") or event.get("event_type") or event.get("event") or "event"
        description = event.get("description") or event.get("page_path") or event.get("session_id") or ""
        values = {**event, "user_id": user.id if user is not None else None, "type": str(event_type), "description": str(description)}
        rows.append(await _api_create(session, "analytics_events", values, user))
    await session.commit()
    return {"data": rows if isinstance(body, list) else (rows[0] if rows else None)}


@router.get("/api/engagement/products/{product_id}/likes")
async def api_product_likes(product_id: uuid.UUID, user: User = Depends(optional_user), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["product_likes"]
    try:
        count = await _count(session, "product_likes", model.product_id == product_id)
        liked = False
        if user is not None:
            liked = bool((await session.execute(select(model.id).where(model.product_id == product_id, model.user_id == user.id).limit(1))).scalar_one_or_none())
    except Exception:
        await session.rollback()
        count = 0
        liked = False
    return {"count": count, "liked": liked}


@router.get("/api/engagement/liked-products")
async def api_liked_products(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["product_likes"]
    try:
        result = await session.execute(select(model).where(model.user_id == user.id).order_by(model.created_at.desc()))
        likes = list(result.scalars())
        product_ids = [like.product_id for like in likes if like.product_id]
        products_by_id: dict[uuid.UUID, dict[str, Any]] = {}
        if product_ids:
            product_result = await session.execute(select(Product).where(Product.id.in_(product_ids)))
            product_rows = await build_public_product_rows(
                session,
                list(product_result.scalars()),
                include_variants=True,
            )
            products_by_id = {uuid.UUID(str(row["id"])): row for row in product_rows if row.get("id")}
        return {
            "data": [
                {
                    **serialize_record(like),
                    # Match the relation shape the wishlist page consumes while
                    # keeping saved likes for products that are no longer public.
                    "products": products_by_id.get(like.product_id),
                }
                for like in likes
            ]
        }
    except Exception:
        await session.rollback()
        return {"data": []}


@router.put("/api/engagement/products/{product_id}/like")
async def api_set_product_like(product_id: uuid.UUID, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    product = (
        await session.execute(
            select(Product).where(Product.id == product_id, *public_product_clauses(Product))
        )
    ).scalar_one_or_none()
    validate_public_product_or_404(product)
    model = MODEL_BY_TABLE["product_likes"]
    body = await request.json()
    liked = bool(body.get("liked", True))
    existing = (await session.execute(select(model).where(model.product_id == product_id, model.user_id == user.id).limit(1))).scalar_one_or_none()
    if liked and existing is None:
        session.add(model(user_id=user.id, product_id=product_id))
    if not liked and existing is not None:
        await session.delete(existing)
    await session.commit()
    return await api_product_likes(product_id, user, session)


@router.post("/api/catalog/admin/categories", status_code=201)
async def api_admin_create_category(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await create_category_record(session, await request.json())
    await session.commit()
    return {"data": row}


@router.patch("/api/catalog/admin/categories/{category_id}")
async def api_admin_update_category(category_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await update_category_record(session, category_id, await request.json())
    await session.commit()
    return {"data": row}


@router.delete("/api/catalog/admin/categories/{category_id}")
async def api_admin_delete_category(category_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    await soft_delete_category_record(session, category_id)
    await session.commit()
    return {"ok": True}


@router.post("/api/catalog/admin/brands", status_code=201)
async def api_admin_create_brand(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _create_resource_row(session, "brands", await request.json())
    await session.commit()
    return {"data": row}


@router.patch("/api/catalog/admin/brands/{brand_id}")
async def api_admin_update_brand(brand_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await session.get(MODEL_BY_TABLE["brands"], brand_id)
    if row is None:
        raise HTTPException(status_code=404, detail="brand_not_found")
    body = await request.json()
    for key, value in body.items():
        if key in row.__table__.c and key not in {"id", "created_at"}:
            setattr(row, key, value)
    await session.commit()
    return {"data": serialize_record(row)}


@router.delete("/api/catalog/admin/brands/{brand_id}")
async def api_admin_delete_brand(brand_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    await session.execute(delete(MODEL_BY_TABLE["brands"]).where(MODEL_BY_TABLE["brands"].id == brand_id))
    await session.commit()
    return {"ok": True}


def _normalize_catalog_mutation_input(body: dict[str, Any]) -> dict[str, Any]:
    values = dict(body)
    aliases = {
        "nameEn": "name_en",
        "richDescription": "rich_description",
        "originalPrice": "original_price",
        "currencyCode": "currency_code",
        "stockQuantity": "stock_quantity",
        "minStockQuantity": "min_stock_quantity",
        "imageUrl": "image_url",
        "categoryId": "category_id",
        "brandId": "brand_id",
        "supplierId": "supplier_id",
        "partnerId": "partner_id",
        "isActive": "is_active",
        "isFeatured": "is_featured",
        "approvalStatus": "approval_status",
        "colorHex": "color_hex",
        "sortOrder": "sort_order",
        "productId": "product_id",
    }
    for source, target in aliases.items():
        if source in values and target not in values:
            values[target] = values.pop(source)
    return values


@router.post("/api/admin/products", status_code=201)
async def api_admin_create_product(request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.create")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail={"code": "product_payload_required", "message": "بيانات المنتج مطلوبة."})
    product_payload = dict(body.get("product") or body)
    variants_payload = body.get("variants")
    if variants_payload is None:
        variants_payload = product_payload.pop("variants", [])
    else:
        product_payload.pop("variants", None)
    if not isinstance(variants_payload, list):
        raise HTTPException(status_code=422, detail={"code": "invalid_variants", "message": "صيغة خيارات المنتج غير صحيحة."})
    product_body = _normalize_catalog_mutation_input(product_payload)
    if product_body.get("partner_id") not in (None, ""):
        partner_id = _uuid(product_body["partner_id"], "partner_id")
        partner_user = await session.get(User, partner_id)
        if partner_user is None or partner_user.deleted_at is not None:
            raise HTTPException(status_code=422, detail={"code": "invalid_partner", "message": "التاجر المحدد غير موجود أو غير متاح."})
        partner_role = await session.get(UserRole, {"user_id": partner_id, "role": "partner"})
        storefront_model = MODEL_BY_TABLE["partner_storefronts"]
        storefront_result = await session.execute(
            select(storefront_model.id)
            .where(
                or_(storefront_model.user_id == partner_id, storefront_model.partner_id == partner_id),
                storefront_model.deleted_at.is_(None),
                storefront_model.is_active.is_(True),
            )
            .limit(1)
        )
        if partner_role is None and storefront_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=422, detail={"code": "invalid_partner", "message": "التاجر المحدد غير مفعّل."})
        product_body["partner_id"] = partner_id
    product_body.setdefault("approval_status", "approved")
    product_body = normalize_product_mutation_values(product_body, partial=False)
    product_body.setdefault("is_active", True)
    if str(product_body.get("approval_status") or "").lower() in {"approved", "active", "published"}:
        product_body.setdefault("approved_by", staff.id)
        product_body.setdefault("approved_at", datetime.now(timezone.utc))
    product = await _create_resource_row(session, "products", product_body)
    variants = []
    for variant_body in variants_payload:
        if not isinstance(variant_body, dict):
            raise HTTPException(status_code=422, detail={"code": "invalid_variant", "message": "بيانات أحد خيارات المنتج غير صحيحة."})
        variant = _normalize_catalog_mutation_input(dict(variant_body))
        variant["product_id"] = product["id"]
        variant = normalize_product_mutation_values(variant, partial=True)
        variants.append(await _create_resource_row(session, "product_variants", variant))
    await session.commit()
    return {"data": {**product, "variants": variants}}


@router.patch("/api/admin/products/{product_id}")
async def api_admin_update_product(product_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.update")
    row = await session.get(MODEL_BY_TABLE["products"], product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    body = normalize_product_mutation_values(_normalize_catalog_mutation_input(await request.json()), partial=True)
    for key, value in body.items():
        if key in row.__table__.c and key not in {"id", "created_at"}:
            setattr(row, key, value)
    await session.commit()
    return {"data": serialize_record(row)}


@router.patch("/api/admin/products/{product_id}/approval")
async def api_admin_product_approval(product_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.activate")
    row = await session.get(MODEL_BY_TABLE["products"], product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    body = await request.json()
    next_status = str(body.get("status") or row.approval_status or "approved").lower()
    if next_status not in {"approved", "active", "published", "pending", "reviewing", "rejected"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_approval_status", "message": "Invalid product approval status"})
    reason = str(
        body.get("reason")
        or body.get("rejectionReason")
        or body.get("approvalNotes")
        or ""
    ).strip()
    if next_status == "rejected" and not reason:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "product_rejection_reason_required",
                "message": "اكتب سبب رفض المنتج قبل الحفظ.",
            },
        )
    row.approval_status = next_status
    if next_status in {"approved", "active", "published"}:
        row.approved_by = staff.id
        row.approved_at = datetime.now(timezone.utc)
        row.is_active = True
        row.approval_notes = None
    else:
        row.approved_by = None
        row.approved_at = None
        if next_status in {"pending", "reviewing", "rejected"}:
            row.is_active = False
        if next_status == "rejected":
            row.approval_notes = reason
        elif next_status in {"pending", "reviewing"}:
            row.approval_notes = None
    if row.partner_id:
        approved = next_status in {"approved", "active", "published"}
        rejected = next_status == "rejected"
        title = (
            "مبروك تم الموافقة على عرض منتجك"
            if approved
            else "تم رفض عرض منتجك"
            if rejected
            else "منتجك قيد المراجعة"
        )
        message = (
            f"تمت الموافقة على عرض المنتج {row.name} ويمكن للعملاء رؤيته الآن."
            if approved
            else f"سبب الرفض: {reason}"
            if rejected
            else f"تم إبقاء المنتج {row.name} قيد المراجعة والتوثيق."
        )
        await NotificationService(session).create_notification(
            NotificationPayload(
                user_id=row.partner_id,
                title=title,
                body=message,
                notification_type=(
                    "product_approved"
                    if approved
                    else "product_rejected"
                    if rejected
                    else "product_submitted_for_review"
                ),
                category="system",
                priority="high",
                action_type="open_product",
                action_url="/partner/products",
                entity_type="products",
                entity_id=str(row.id),
                payload={
                    "productId": str(row.id),
                    "approvalStatus": next_status,
                    "deep_link": "/partner/products",
                    **({"rejectionReason": reason} if rejected else {}),
                },
                created_by=staff.id,
                deduplication_key=f"product-review:{row.id}:{next_status}:{row.approved_at or reason}",
            )
        )
    await session.commit()
    return {"data": serialize_record(row)}


@router.patch("/api/catalog/admin/variants/{variant_id}")
async def api_admin_update_variant(variant_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.update")
    row = await session.get(MODEL_BY_TABLE["product_variants"], variant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="variant_not_found")
    body = normalize_product_mutation_values(_normalize_catalog_mutation_input(await request.json()), partial=True)
    if "product_id" in body:
        try:
            requested_product_id = uuid.UUID(str(body["product_id"]))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"code": "invalid_product_id", "message": "Invalid product reference"})
        if requested_product_id != row.product_id:
            raise HTTPException(status_code=403, detail="variant_product_mismatch")
    for key, value in body.items():
        if key in row.__table__.c and key not in {"id", "created_at", "product_id"}:
            setattr(row, key, value)
    await session.commit()
    return {"data": serialize_record(row)}


@router.delete("/api/admin/products/{product_id}")
async def api_admin_delete_product(product_id: uuid.UUID, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, staff.id, roles, "products.delete")
    row = await session.get(MODEL_BY_TABLE["products"], product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    now = datetime.now(timezone.utc)
    row.is_active = False
    if hasattr(row, "is_featured"):
        row.is_featured = False
    if hasattr(row, "approval_status"):
        row.approval_status = "deleted"
    if hasattr(row, "deleted_at"):
        row.deleted_at = now
    variants = list(
        (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == product_id,
                    ProductVariant.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    for variant in variants:
        variant.is_active = False
        variant.deleted_at = now
    removed_assets = await _delete_product_file_assets(
        session,
        product=row,
        variants=variants,
        actor=staff,
    )
    await session.commit()
    return {"ok": True, "removed_assets": removed_assets, "data": serialize_record(row)}


def _normalize_admin_body(
    table: str,
    body: dict[str, Any],
    actor: User | None = None,
    *,
    for_create: bool = False,
) -> dict[str, Any]:
    values = dict(body)
    if table in {"suppliers", "local_merchants"}:
        # Supplier/merchant forms are shared by the web dashboard and the
        # Flutter dashboard. Keep both naming conventions in one backend
        # contract and persist fields that are not physical columns in the
        # resource's JSON extra_data through _create_resource_row/_api_update.
        aliases = {
            "business_name": "name",
            "nameEn": "name_en",
            "whatsappNumber": "whatsapp_number",
            "supplierType": "supplier_type",
            "isActive": "is_active",
            "logoUrl": "logo_url",
        }
        for source, target in aliases.items():
            if source in values and target not in values:
                values[target] = values.pop(source)
        if for_create:
            name = str(values.get("name") or "").strip()
            if len(name) < 2:
                raise HTTPException(status_code=422, detail="supplier_name_required")
            values["name"] = name
            values.setdefault("supplier_type", "merchant" if table == "local_merchants" else "supplier")
            values.setdefault("status", "active" if values.get("is_active", True) else "inactive")
        elif "is_active" in values and "status" not in values:
            values["status"] = "active" if values["is_active"] else "inactive"
        supplier_type = values.get("supplier_type")
        if supplier_type is not None:
            supplier_type = str(supplier_type).strip().lower()
            if supplier_type not in {"supplier", "merchant", "both"}:
                raise HTTPException(status_code=422, detail="invalid_supplier_type")
            values["supplier_type"] = supplier_type
        images = values.get("images")
        if isinstance(images, str):
            values["images"] = [images.strip()] if images.strip() else []
        elif images is not None:
            values["images"] = [str(item).strip() for item in images if str(item).strip()] if isinstance(images, list) else []
    if table == "couriers":
        aliases = {
            "full_name": "name",
            "isActive": "is_active",
            "vehicleType": "vehicle_type",
            "coverageArea": "coverage_area",
        }
        for source, target in aliases.items():
            if source in values and target not in values:
                values[target] = values.pop(source)
        if for_create or "name" in values:
            name = str(values.get("name") or "").strip()
            if len(name) < 2:
                raise HTTPException(status_code=422, detail="courier_name_required")
            values["name"] = name
        if for_create:
            values.setdefault(
                "status",
                "active" if values.get("is_active", True) else "inactive",
            )
        elif "is_active" in values and "status" not in values:
            values["status"] = "active" if values["is_active"] else "inactive"
    if table == "shipping_carriers":
        if "base_cost" in values and "fee" not in values:
            values["fee"] = values["base_cost"]
        values.setdefault("status", "active" if values.get("is_active", True) else "inactive")
    if table == "shipping_stages":
        if "stage_name" in values and "name" not in values:
            values["name"] = values["stage_name"]
        if "stage_code" in values and "code" not in values:
            values["code"] = values["stage_code"]
        values.setdefault("status", "active")
    if table == "form_settings":
        if "form_key" in values and "name" not in values:
            values["name"] = values["form_key"]
        if "form_name" in values and "type" not in values:
            values["type"] = values["form_name"]
        values.setdefault("status", "active")
    if table == "banners":
        if "link_url" in values and "url" not in values:
            values["url"] = values["link_url"]
        if for_create or "is_active" in values:
            active = values.get("is_active", True)
            if isinstance(active, str):
                active = active.strip().lower() not in {"false", "0", "no", "inactive"}
            values.setdefault("status", "active" if active else "inactive")
    if table == "shipping_zones":
        if "governorate" in values and "name" not in values:
            values["name"] = values["governorate"]
        values.setdefault("status", "active")
    if table == "blog_articles":
        if "content" in values and "body" not in values:
            values["body"] = values["content"]
        values.setdefault("status", "draft")
    if table == "static_pages":
        if "content" in values and "body" not in values:
            values["body"] = values["content"]
        if "is_published" in values and "is_active" not in values:
            values["is_active"] = values["is_published"]
        if for_create or "is_published" in values or "is_active" in values:
            active = values.get("is_active", False)
            if isinstance(active, str):
                active = active.strip().lower() not in {"false", "0", "no", "draft", "inactive"}
            values.setdefault("status", "active" if active else "draft")
    if table == "page_sections":
        if "section_name" in values and "title" not in values:
            values["title"] = values["section_name"]
        if "content" in values and "body" not in values:
            values["body"] = values["content"]
        if "is_visible" in values and "is_active" not in values:
            values["is_active"] = values["is_visible"]
        if for_create or "is_visible" in values or "is_active" in values:
            active = values.get("is_active", True)
            if isinstance(active, str):
                active = active.strip().lower() not in {"false", "0", "no", "inactive"}
            values.setdefault("status", "active" if active else "inactive")
    if table == "custom_elements":
        title = str(values.get("title") or values.get("name") or "").strip()
        if title and "name" not in values:
            values["name"] = title
        elif for_create and "name" not in values:
            values["name"] = "custom_element"
        element_type = str(values.get("element_type") or values.get("type") or "").strip()
        if element_type and "type" not in values:
            values["type"] = element_type
        elif for_create and "type" not in values:
            values["type"] = "custom_html"
        if "content" in values and "body" not in values:
            values["body"] = values["content"]
        if "is_visible" in values and "is_active" not in values:
            values["is_active"] = values["is_visible"]
        if for_create or "is_visible" in values or "is_active" in values:
            active = values.get("is_active", True)
            if isinstance(active, str):
                active = active.strip().lower() not in {"false", "0", "no", "inactive"}
            values.setdefault("status", "active" if active else "inactive")
    if table == "coupons":
        if "discount_value" in values and "amount" not in values:
            values["amount"] = values["discount_value"]
        values.setdefault("title", str(values.get("code") or "Coupon"))
        values.setdefault("status", "active" if values.get("is_active", True) else "inactive")
    if table == "marketing_campaigns":
        values.setdefault("status", "active" if values.get("is_active", True) else "draft")
        if actor is not None:
            values.setdefault("created_by", actor.id)
    if table in {"employee_payments", "cash_transactions", "financial_vouchers", "general_expenses", "risk_alerts"}:
        if actor is not None:
            values.setdefault("user_id", actor.id)
        if "payment_type" in values and "type" not in values:
            values["type"] = values["payment_type"]
        if "transaction_type" in values and "type" not in values:
            values["type"] = values["transaction_type"]
        if "voucher_type" in values and "type" not in values:
            values["type"] = values["voucher_type"]
        if "expense_category" in values and "type" not in values:
            values["type"] = values["expense_category"]
        if "alert_type" in values and "type" not in values:
            values["type"] = values["alert_type"]
        values.setdefault("status", "pending")
    return values


async def _api_create(session: AsyncSession, table: str, body: dict[str, Any], actor: User | None = None) -> dict[str, Any]:
    return await _create_resource_row(session, table, _normalize_admin_body(table, body, actor, for_create=True))


async def _api_update(session: AsyncSession, table: str, record_id: uuid.UUID, body: dict[str, Any], actor: User | None = None) -> dict[str, Any]:
    if table == "categories":
        return await update_category_record(session, record_id, _normalize_admin_body(table, body, actor))
    model = MODEL_BY_TABLE[table]
    row = await session.get(model, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="record_not_found")
    values = _normalize_admin_body(table, body, actor)
    extra = dict(getattr(row, "extra_data", {}) or {})
    for key, value in values.items():
        if key in model.__table__.c and key not in {"id", "created_at"}:
            setattr(row, key, value)
        else:
            extra[key] = _jsonable(value)
    if "extra_data" in model.__table__.c:
        row.extra_data = extra
    await session.flush()
    return serialize_record(row)


async def _api_delete(session: AsyncSession, table: str, record_id: uuid.UUID) -> None:
    if table == "categories":
        await soft_delete_category_record(session, record_id)
        return
    model = MODEL_BY_TABLE[table]
    await session.execute(delete(model).where(model.id == record_id))


def _validate_payment_record_body(body: dict[str, Any], *, allow_status: bool = True) -> None:
    for field in DIRECT_RECEIPT_INPUT_FIELDS:
        value = body.get(field)
        if field in {"receipt_url", "receiptUrl"} and value is not None and str(value).strip().startswith("file:"):
            continue
        if value is not None and str(value).strip():
            raise HTTPException(status_code=422, detail="payment_receipts_must_use_order_endpoint")
    if allow_status and "status" in body:
        status_value = str(body.get("status") or "").strip().lower()
        if status_value and status_value not in PAYMENT_RECORD_STATUSES:
            raise HTTPException(status_code=422, detail="invalid_payment_status")
        body["status"] = status_value or "pending"
    for untrusted in ("user_id", "userId", "payment_status", "paymentStatus", "refund_status", "refundStatus"):
        if untrusted in body:
            raise HTTPException(status_code=422, detail=f"untrusted_financial_field:{untrusted}")


@router.post("/api/catalog/admin/currencies", status_code=201)
async def api_admin_create_currency(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _api_create(session, "currencies", await request.json(), staff)
    await session.commit()
    return {"data": row}


@router.patch("/api/catalog/admin/currencies/{record_id}")
async def api_admin_update_currency(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _api_update(session, "currencies", record_id, await request.json(), staff)
    await session.commit()
    return {"data": row}


@router.delete("/api/catalog/admin/currencies/{record_id}")
async def api_admin_delete_currency(record_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    await _api_delete(session, "currencies", record_id)
    await session.commit()
    return {"ok": True}


@router.post("/api/catalog/admin/banners", status_code=201)
async def api_admin_create_banner(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _api_create(session, "banners", await request.json(), staff)
    if "banner_history" in MODEL_BY_TABLE:
        session.add(MODEL_BY_TABLE["banner_history"](title=row.get("title"), image_url=row.get("image_url"), status="created", created_by=staff.id, extra_data={"banner_id": row.get("id")}))
    await session.commit()
    return {"data": row}


@router.patch("/api/catalog/admin/banners/{record_id}")
async def api_admin_update_banner(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _api_update(session, "banners", record_id, await request.json(), staff)
    if "banner_history" in MODEL_BY_TABLE:
        session.add(MODEL_BY_TABLE["banner_history"](title=row.get("title"), image_url=row.get("image_url"), status="updated", created_by=staff.id, extra_data={"banner_id": str(record_id)}))
    await session.commit()
    return {"data": row}


@router.get("/api/catalog/admin/banners/{record_id}/history")
async def api_admin_banner_history(record_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["banner_history"]
    result = await session.execute(select(model).where(model.extra_data["banner_id"].astext == str(record_id)).order_by(model.created_at.desc()))
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.delete("/api/catalog/admin/banners/{record_id}")
async def api_admin_delete_banner(record_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    await _api_delete(session, "banners", record_id)
    await session.commit()
    return {"ok": True}


async def _create_update_delete_resource(table: str, request: Request, session: AsyncSession, staff: User, record_id: uuid.UUID | None = None, action: str = "create"):
    if action == "create":
        row = await _api_create(session, table, await request.json(), staff)
        await session.commit()
        return {"data": row}
    if action == "update" and record_id is not None:
        row = await _api_update(session, table, record_id, await request.json(), staff)
        await session.commit()
        return {"data": row}
    if action == "delete" and record_id is not None:
        await _api_delete(session, table, record_id)
        await session.commit()
        return {"ok": True}
    raise HTTPException(status_code=400, detail="invalid_resource_action")


@router.get("/api/catalog/banners")
async def api_catalog_banners(position: str | None = None, session: AsyncSession = Depends(get_session)):
    rows = await _resource_data(session, "banners")
    if position:
        rows = [row for row in rows if not row.get("position") or row.get("position") == position or (row.get("extra_data") or {}).get("position") == position]
    return {"data": rows}


@router.post("/api/catalog/banners/{banner_id}/events")
async def api_catalog_banner_event(banner_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE.get("analytics_events")
    if model is not None:
        session.add(model(type=f"banner_{body.get('type') or 'event'}", entity_id=banner_id, extra_data=body))
        await session.commit()
    return {"tracked": True}


@router.post("/api/admin/global-sites", status_code=201)
async def api_create_global_site(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("global_sites", request, session, staff)


@router.patch("/api/admin/global-sites/{record_id}")
async def api_update_global_site(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("global_sites", request, session, staff, record_id, "update")


@router.delete("/api/admin/global-sites/{record_id}")
async def api_delete_global_site(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("global_sites", request, session, staff, record_id, "delete")


@router.post("/api/admin/local-merchants", status_code=201)
async def api_create_local_merchant(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("local_merchants", request, session, staff)


@router.patch("/api/admin/local-merchants/{record_id}")
async def api_update_local_merchant(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("local_merchants", request, session, staff, record_id, "update")


@router.delete("/api/admin/local-merchants/{record_id}")
async def api_delete_local_merchant(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("local_merchants", request, session, staff, record_id, "delete")


@router.post("/api/admin/warehouses", status_code=201)
async def api_create_warehouse(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("warehouses", request, session, staff)


@router.patch("/api/admin/warehouses/{record_id}")
async def api_update_warehouse(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("warehouses", request, session, staff, record_id, "update")


@router.delete("/api/admin/warehouses/{record_id}")
async def api_delete_warehouse(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("warehouses", request, session, staff, record_id, "delete")


@router.post("/api/admin/couriers", status_code=201)
async def api_create_admin_courier(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("couriers", request, session, staff)


@router.patch("/api/admin/couriers/{record_id}")
async def api_update_admin_courier(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("couriers", request, session, staff, record_id, "update")


@router.delete("/api/admin/couriers/{record_id}")
async def api_delete_admin_courier(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    assignment_model = MODEL_BY_TABLE["courier_assignments"]
    active_assignment = await session.execute(
        select(assignment_model.id).where(
            or_(assignment_model.courier_id == record_id, assignment_model.user_id == record_id),
            assignment_model.deleted_at.is_(None),
            assignment_model.status.in_(("active", "assigned", "accepted", "picked_up", "out_for_delivery")),
        ).limit(1)
    )
    if active_assignment.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="courier_has_active_assignments")
    return await _create_update_delete_resource("couriers", request, session, staff, record_id, "delete")


@router.post("/api/suppliers", status_code=201)
async def api_create_supplier(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("suppliers", request, session, staff)


@router.patch("/api/suppliers/{record_id}")
async def api_update_supplier(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("suppliers", request, session, staff, record_id, "update")


@router.delete("/api/suppliers/{record_id}")
async def api_delete_supplier(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("suppliers", request, session, staff, record_id, "delete")


@router.get("/api/content/settings/public/{setting_key}")
async def api_content_public_setting(setting_key: str, session: AsyncSession = Depends(get_session)):
    return _public_setting_payload(await _resource_data(session, "site_settings"), setting_key)


@router.get("/api/content/settings/admin")
async def api_content_admin_settings(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": await _resource_data(session, "site_settings")}


@router.patch("/api/content/settings/{setting_key}")
async def api_content_update_setting(setting_key: str, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE["site_settings"]
    rows = await _rows(session, "site_settings", clauses=(model.name == setting_key,), limit=1)
    row = rows[0] if rows else model(name=setting_key, status="active", is_active=True)
    if not rows:
        session.add(row)
    row.extra_data = body.get("value") if isinstance(body.get("value"), dict) else body
    await session.commit()
    return {"data": serialize_record(row)}


@router.put("/api/content/sections/reorder")
async def api_content_reorder_sections(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE["page_sections"]
    updated = []
    for item in body.get("sections", []):
        row = await session.get(model, uuid.UUID(str(item.get("id"))))
        if row is not None:
            row.sort_order = int(item.get("sort_order") or row.sort_order or 0)
            updated.append(serialize_record(row))
    await session.commit()
    return {"data": updated}


@router.patch("/api/content/sections/{record_id}")
async def api_content_patch_section_alias(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await api_content_update_section("sections", record_id, request, staff, set(), session)


@router.patch("/api/content/theme/{setting_key}")
async def api_content_patch_theme_setting(
    setting_key: str,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await ThemeAdminService().save(session, actor=staff, roles=roles, body=await request.json(), setting_key=setting_key, publish=True)}


@router.get("/api/content/theme/history")
async def api_content_theme_history(
    limit: int = Query(default=50, ge=1, le=200),
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    """Return the saved theme versions used by the design-control history tab."""
    ThemeAdminService.require_access(roles)
    model = MODEL_BY_TABLE["theme_settings"]
    rows = await _rows(session, "theme_settings", clauses=(model.status == "history",), limit=limit)
    history: list[dict[str, Any]] = []
    for row in rows:
        extra = dict(row.extra_data or {})
        changed_at = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
        history.append(
            {
                "id": str(row.id),
                "setting_key": str(extra.get("setting_key") or "default"),
                "old_value": _jsonable(extra.get("old_value")),
                "new_value": _jsonable(extra.get("new_value")),
                "changed_by": str(extra.get("changed_by") or extra.get("updated_by") or ""),
                "changed_at": changed_at.isoformat() if changed_at else None,
                "description": str(extra.get("description") or ""),
            }
        )
    return {"data": history}


@router.post("/api/content/theme/history", status_code=201)
async def api_content_create_theme_history(request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    ThemeAdminService.require_access(roles)
    body = await request.json()
    row = await _api_create(session, "theme_settings", {
        "name": f"history:{body.get('setting_key') or uuid.uuid4()}",
        "status": "history",
        "is_active": False,
        "description": body.get("description") or "",
        **body,
    }, staff)
    await session.commit()
    return {"data": row}


@router.post("/api/content/theme/history/{history_id}/revert")
async def api_content_revert_theme_history(history_id: uuid.UUID, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    ThemeAdminService.require_access(roles)
    model = MODEL_BY_TABLE["theme_settings"]
    history = await session.get(model, history_id)
    if history is None:
        raise HTTPException(status_code=404, detail="history_not_found")
    extra = dict(history.extra_data or {})
    key = extra.get("setting_key")
    if key:
        rows = await _rows(session, "theme_settings", clauses=(model.name == str(key),), limit=1)
        row = rows[0] if rows else model(name=str(key), status="active", is_active=True, extra_data={})
        if not rows:
            session.add(row)
        row.extra_data = {"key": str(key), "value": _jsonable(extra.get("old_value", {}))}
    await session.commit()
    return {"data": serialize_record(history)}


@router.get("/api/content/theme/templates")
async def api_content_theme_templates(
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    """List persisted design templates without mixing them with live settings."""
    ThemeAdminService.require_access(roles)
    model = MODEL_BY_TABLE["theme_settings"]
    rows = await _rows(
        session,
        "theme_settings",
        clauses=(or_(model.status == "template", model.name.like("template:%")),),
        limit=100,
    )
    templates: list[dict[str, Any]] = []
    for row in rows:
        record = serialize_record(row)
        extra = dict(row.extra_data or {})
        settings = extra.get("settings") if isinstance(extra.get("settings"), dict) else extra
        raw_name = str(extra.get("name") or record.get("name") or "")
        templates.append(
            {
                "id": str(row.id),
                "name": raw_name.removeprefix("template:") or "قالب تصميم",
                "name_en": extra.get("name_en"),
                "description": extra.get("description") or record.get("description"),
                "preview_image": extra.get("preview_image") or extra.get("previewImage"),
                "settings": _jsonable(settings),
                "is_active": bool(record.get("is_active") is not False),
                "is_default": bool(extra.get("is_default") or extra.get("isDefault")),
                "sort_order": int(extra.get("sort_order") or record.get("sort_order") or 0),
                "created_at": record.get("created_at"),
            }
        )
    templates.sort(key=lambda item: (item["sort_order"], item["name"]))
    if not templates:
        # Keep the admin design panel usable on a fresh database. These are
        # read-only built-ins; applying one still persists the selected theme
        # through the normal authenticated update path.
        templates = [
            {
                "id": "builtin-luxury-light",
                "name": "الفخامة الهادئة",
                "name_en": "Quiet Luxury",
                "description": "واجهة فاتحة راقية تبرز المنتجات والعروض بوضوح وتوازن.",
                "preview_image": None,
                "settings": {
                    "colors": {"primary": "43 85% 50%", "background": "0 0% 100%", "foreground": "220 14% 10%", "card": "0 0% 100%", "gold": "43 85% 50%", "goldLight": "45 88% 66%", "goldDark": "43 82% 32%"},
                    "typography": {"fontFamily": "Cairo", "headingSize": "1.5", "bodySize": "1"},
                    "layout": {"containerWidth": "1400px", "sectionPadding": "6rem", "borderRadius": "0.75rem"},
                    "components": {"cardRadius": "1rem", "cardShadow": "elegant", "cardHover": True},
                    "animations": {"enabled": True, "duration": "0.3s", "type": "fade"},
                },
                "is_active": True,
                "is_default": True,
                "sort_order": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "builtin-midnight-luxury",
                "name": "ليالي الرفاهية",
                "name_en": "Midnight Luxury",
                "description": "ثيم داكن سينمائي يمنح المتجر حضوراً فاخراً مع تباين مريح.",
                "preview_image": None,
                "settings": {
                    "colors": {"primary": "43 85% 50%", "background": "220 14% 8%", "foreground": "0 0% 96%", "card": "220 13% 13%", "gold": "43 85% 50%", "goldLight": "45 88% 66%", "goldDark": "43 82% 32%"},
                    "typography": {"fontFamily": "Cairo", "headingSize": "1.55", "bodySize": "1"},
                    "layout": {"containerWidth": "1400px", "sectionPadding": "6rem", "borderRadius": "0.75rem"},
                    "components": {"cardRadius": "1rem", "cardShadow": "dramatic", "cardHover": True},
                    "animations": {"enabled": True, "duration": "0.35s", "type": "fade"},
                },
                "is_active": True,
                "is_default": False,
                "sort_order": 2,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "builtin-ramadan-gold",
                "name": "رمضان الذهبي",
                "name_en": "Ramadan Gold",
                "description": "ثيم موسمي دافئ للمناسبات، مع لمسة ذهبية ورسالة ترحيبية واضحة.",
                "preview_image": None,
                "settings": {
                    "colors": {"primary": "43 85% 50%", "background": "270 25% 10%", "foreground": "0 0% 96%", "card": "270 20% 16%", "gold": "43 85% 50%", "goldLight": "45 88% 66%", "goldDark": "43 82% 32%"},
                    "typography": {"fontFamily": "Cairo", "headingSize": "1.5", "bodySize": "1"},
                    "layout": {"containerWidth": "1400px", "sectionPadding": "6rem", "borderRadius": "1rem"},
                    "components": {"cardRadius": "1rem", "cardShadow": "elegant", "cardHover": True},
                    "animations": {"enabled": True, "duration": "0.4s", "type": "spring"},
                    "seasonal": {"event": "ramadan", "greeting": "رمضان كريم"},
                },
                "is_active": True,
                "is_default": False,
                "sort_order": 3,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        ]
    return {"data": templates}


@router.post("/api/content/theme/templates/{template_id}/apply")
async def api_content_apply_theme_template(template_id: uuid.UUID, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    ThemeAdminService.require_access(roles)
    model = MODEL_BY_TABLE["theme_settings"]
    template = await session.get(model, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="template_not_found")
    template_extra = dict(template.extra_data or {})
    template_settings = template_extra.get("settings") if isinstance(template_extra.get("settings"), dict) else template_extra
    if isinstance(template_settings, dict) and isinstance(template_settings.get("settings"), dict):
        template_settings = template_settings["settings"]
    setting_keys = {
        "colors": "colors",
        "typography": "typography",
        "layout": "layout",
        "components": "cards",
        "cards": "cards",
        "buttons": "buttons",
        "inputs": "inputs",
        "animations": "animations",
        "hero": "hero",
    }
    updates = {
        setting_keys[key]: value
        for key, value in (template_settings.items() if isinstance(template_settings, dict) else [])
        if key in setting_keys and value is not None
    }
    if not updates:
        raise HTTPException(status_code=422, detail="template_has_no_theme_settings")
    theme_service = ThemeAdminService()
    for setting_key, value in updates.items():
        # Apply each setting through the same audited publish path used by the
        # design panel, so this endpoint cannot leave the public site unchanged.
        await theme_service.save(
            session,
            actor=staff,
            roles=roles,
            body={"value": _jsonable(value)},
            setting_key=setting_key,
            publish=True,
        )
    rows = await _rows(session, "theme_settings", clauses=(model.name == "active_template",), limit=1)
    row = rows[0] if rows else model(name="active_template", status="active", is_active=True, extra_data={})
    if not rows:
        session.add(row)
    row.extra_data = {"template_id": str(template_id), "settings": _jsonable(updates), "applied_at": _now().isoformat(), "updated_by": str(staff.id)}
    await session.commit()
    return {"data": serialize_record(row)}


@router.post("/api/content/theme/preview")
async def api_content_theme_preview(
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await ThemeAdminService().preview(session, actor=staff, roles=roles, body=await request.json())


@router.get("/api/content/theme/preview/{token}")
async def api_content_theme_preview_token(token: str, session: AsyncSession = Depends(get_session)):
    return {"data": await ThemeAdminService().public_preview(session, token=token)}


@router.get("/api/content/pages/{page_id}/versions")
async def api_content_page_versions(page_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["page_versions"]
    rows = await _rows(session, "page_versions", clauses=(model.extra_data["page_id"].astext == str(page_id),), limit=100)
    return {"data": [serialize_record(row) for row in rows]}


@router.post("/api/content/pages/{page_id}/restore")
async def api_content_restore_page(page_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    version = body.get("version") if isinstance(body.get("version"), dict) else body
    model = MODEL_BY_TABLE["static_pages"]
    row = await session.get(model, page_id)
    if row is None:
        raise HTTPException(status_code=404, detail="page_not_found")
    if version.get("title"):
        row.title = str(version["title"])
    if version.get("content") or version.get("body"):
        row.body = str(version.get("content") or version.get("body"))
    await session.commit()
    return {"data": serialize_record(row)}


@router.get("/api/content/blog/{slug}")
async def api_content_blog_article(slug: str, session: AsyncSession = Depends(get_session)):
    articles = [_blog_article_payload(row) for row in await _blog_source_rows(session)]
    article = next((row for row in articles if row.get("slug") == slug and row.get("is_published") is True), None)
    if article is None:
        raise HTTPException(status_code=404, detail="article_not_found")
    return {"data": article}


@router.patch("/api/content/site/{content_key}")
async def api_content_update_site(
    content_key: str,
    request: Request,
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    """Update one editable site-content block by its stable key."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="site_content_payload_required")

    key = str(content_key or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="site_content_key_required")

    model = MODEL_BY_TABLE["site_content"]
    rows = await _rows(session, "site_content", clauses=(model.name == key,), limit=1)
    row = rows[0] if rows else model(name=key, status="active")
    if not rows:
        session.add(row)

    extra = dict(getattr(row, "extra_data", None) or {})
    for field, value in body.items():
        if field in {"key", "name"}:
            continue
        if field in {"content", "body"}:
            row.body = _jsonable(value)
        elif field == "title":
            row.title = _jsonable(value)
        elif field in {"image_url", "imageUrl"}:
            extra["image_url"] = _jsonable(value)
        elif field in {"metadata", "meta"}:
            extra["metadata"] = _jsonable(value)
        elif field == "status":
            row.status = str(value or "active")
        else:
            extra[field] = _jsonable(value)

    row.status = str(getattr(row, "status", None) or "active")
    row.extra_data = extra
    await session.commit()
    return {"data": _site_content_payload(row)}


@router.get("/api/content/{section_key}")
async def api_content_section(section_key: str, page: str | None = None, key: str | None = None, category: str | None = None, admin: bool = False, user: User | None = Depends(public_optional_user), session: AsyncSession = Depends(get_session)):
    table = CONTENT_TABLES.get(section_key)
    if table is None:
        raise HTTPException(status_code=404, detail="content_section_not_found")
    if admin:
        if user is None:
            raise HTTPException(status_code=401, detail="authentication_required")
        role_rows = await session.execute(select(UserRole.role).where(UserRole.user_id == user.id))
        if not set(role_rows.scalars()).intersection({"admin", "manager", "staff", "employee"}):
            raise HTTPException(status_code=403, detail="insufficient_permissions")
        if section_key == "sections" and (page or "home") == "home":
            await _ensure_home_page_sections(session)
    rows = await _resource_data(session, table)
    if section_key == "blog":
        rows = [_blog_article_payload(row) for row in await _blog_source_rows(session)]
        if not admin:
            rows = [row for row in rows if row.get("is_published") is True]
        if category and category != "all":
            rows = [row for row in rows if row.get("category") == category]
        return {"data": rows}
    if not admin:
        rows = _public_content_rows(rows)
        if section_key == "theme":
            rows = [
                row
                for row in rows
                if not str(row.get("name") or "").startswith(("history:", "preview:"))
            ]
    if page:
        rows = [row for row in rows if row.get("page") == page or (row.get("extra_data") or {}).get("page") == page]
    if section_key == "sections":
        rows = sorted(rows, key=lambda row: (int(row.get("sort_order") or 0), str(row.get("section_name") or row.get("title") or "")))
    if key:
        rows = [row for row in rows if row.get("name") == key or row.get("form_key") == key or (row.get("extra_data") or {}).get("form_key") == key]
        if section_key == "forms":
            return {"data": rows[0] if rows else None}
    return {"data": rows}


@router.post("/api/content/{section_key}", status_code=201)
async def api_content_create_section(section_key: str, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    table = CONTENT_TABLES.get(section_key)
    if table is None:
        raise HTTPException(status_code=404, detail="content_section_not_found")
    body = await request.json()
    if section_key == "blog":
        values, extra = _normalize_blog_body(body, for_create=True, table="blog_articles")
        row = MODEL_BY_TABLE["blog_articles"](**values, extra_data=extra)
        session.add(row)
        await session.commit()
        return {"data": _blog_article_payload(row)}
    body = _normalize_admin_body(table, body, staff, for_create=True)
    if table == "form_settings":
        FormSettingsPersistenceService.validate(body)
        body = {**body, "name": str(body.get("form_key") or body.get("formKey") or body.get("name")), "status": "active"}
    result = await _create_resource_row(session, table, body)
    await session.commit()
    return {"data": result}


@router.patch("/api/content/{section_key}/{record_id}")
async def api_content_update_section(section_key: str, record_id: str, request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    table = CONTENT_TABLES.get(section_key)
    if table is None:
        raise HTTPException(status_code=404, detail="content_section_not_found")
    if section_key == "blog":
        try:
            parsed_id = uuid.UUID(str(record_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_uuid:record_id")
        row = await session.get(MODEL_BY_TABLE["blog_articles"], parsed_id)
        if row is None:
            row = await session.get(MODEL_BY_TABLE["site_content"], parsed_id)
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="record_not_found")
        values, extra = _normalize_blog_body(await request.json(), existing=row, table=row.__table__.name)
        for key, value in values.items():
            setattr(row, key, value)
        row.extra_data = extra
        await session.commit()
        return {"data": _blog_article_payload(row)}
    model = MODEL_BY_TABLE[table]
    row = None
    try:
        row = await session.get(model, uuid.UUID(str(record_id)))
    except ValueError:
        if table == "form_settings":
            row = (
                await session.execute(
                    select(model)
                    .where(
                        model.deleted_at.is_(None),
                        or_(model.name == record_id, model.extra_data["form_key"].astext == record_id, model.extra_data["formKey"].astext == record_id),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="record_not_found")
    body = _normalize_admin_body(table, await request.json(), staff, for_create=False)
    if table == "form_settings":
        FormSettingsPersistenceService.validate(body, form_key=getattr(row, "name", None))
        active = body.get("is_active", getattr(row, "is_active", True))
        if isinstance(active, str):
            active = active.strip().lower() not in {"false", "0", "no", "inactive"}
        body = {**body, "name": getattr(row, "name", None) or str(body.get("form_key") or body.get("formKey") or record_id), "status": "active" if active else "inactive"}
    extra = dict(getattr(row, "extra_data", {}) or {})
    for key, value in body.items():
        if key in model.__table__.c and key not in {"id", "created_at"}:
            setattr(row, key, value)
        else:
            extra[key] = value
    if "extra_data" in model.__table__.c:
        row.extra_data = extra
    await session.commit()
    return {"data": serialize_record(row)}


@router.delete("/api/content/{section_key}/{record_id}")
async def api_content_delete_section(section_key: str, record_id: uuid.UUID, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    table = CONTENT_TABLES.get(section_key)
    if table is None:
        raise HTTPException(status_code=404, detail="content_section_not_found")
    if section_key == "blog":
        row = await session.get(MODEL_BY_TABLE["blog_articles"], record_id)
        if row is None:
            row = await session.get(MODEL_BY_TABLE["site_content"], record_id)
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="record_not_found")
        row.deleted_at = datetime.now(timezone.utc)
        await session.commit()
        return {"ok": True, "data": _blog_article_payload(row)}
    result = await ResourceRepository(session, table, staff.id, roles).delete({
        "filters": [{"column": "id", "operator": "eq", "value": str(record_id)}],
    })
    await session.commit()
    return {"ok": True, "data": result}


@router.get("/admin/sections/{section_key}/records")
async def admin_section_records(section_key: str, limit: int = 500, admin: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    table = SECTION_TABLES.get(section_key, section_key.replace("-", "_"))
    if table not in RESOURCE_TABLES:
        raise HTTPException(status_code=404, detail="section_not_found")
    repository = ResourceRepository(session, table, admin.id, roles)
    repository.ensure_access("select")
    return [serialize_record(row) for row in await _rows(session, table, limit=limit)]


@router.post("/admin/sections/{section_key}/records")
async def create_admin_section(section_key: str, request: Request, admin: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    table = SECTION_TABLES.get(section_key, section_key.replace("-", "_"))
    if table not in RESOURCE_TABLES:
        raise HTTPException(status_code=404, detail="section_not_found")
    ResourceRepository(session, table, admin.id, roles).ensure_access("insert")
    body = await request.json()
    if table == "categories":
        row_data = await create_category_record(session, body)
        _add_audit_log(session, admin.id, f"admin.{table}.create", f"Created {table} record")
        await session.commit()
        return row_data
    if table in {"suppliers", "local_merchants"}:
        row_data = await _api_create(session, table, body, admin)
        _add_audit_log(session, admin.id, f"admin.{table}.create", f"Created {table} record")
        await session.commit()
        return row_data
    model = MODEL_BY_TABLE[table]
    values = {key: value for key, value in body.items() if key in model.__table__.c and key not in {"id", "created_at", "updated_at", "deleted_at", "extra_data"}}
    extra = {key: value for key, value in body.items() if key not in model.__table__.c}
    row = model(**values, extra_data=extra) if "extra_data" in model.__table__.c else model(**values)
    session.add(row)
    _add_audit_log(session, admin.id, f"admin.{table}.create", f"Created {table} record")
    await session.commit()
    return serialize_record(row)


@router.patch("/admin/sections/{section_key}/records/{record_id}")
async def update_admin_section(section_key: str, record_id: uuid.UUID, request: Request, admin: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    table = SECTION_TABLES.get(section_key, section_key.replace("-", "_"))
    if table not in RESOURCE_TABLES:
        raise HTTPException(status_code=404, detail="section_not_found")
    ResourceRepository(session, table, admin.id, roles).ensure_access("update")
    if table == "categories":
        row_data = await update_category_record(session, record_id, await request.json())
        _add_audit_log(session, admin.id, f"admin.{table}.update", f"Updated {table} record {record_id}")
        await session.commit()
        return row_data
    if table in {"suppliers", "local_merchants"}:
        row_data = await _api_update(session, table, record_id, await request.json(), admin)
        _add_audit_log(session, admin.id, f"admin.{table}.update", f"Updated {table} record {record_id}")
        await session.commit()
        return row_data
    model = MODEL_BY_TABLE[table]
    row = await session.get(model, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="record_not_found")
    body = await request.json()
    extra = dict(getattr(row, "extra_data", {}) or {})
    for key, value in body.items():
        if key in model.__table__.c and key not in {"id", "created_at"}:
            setattr(row, key, value)
        else:
            extra[key] = value
    if hasattr(row, "extra_data"):
        row.extra_data = extra
    _add_audit_log(session, admin.id, f"admin.{table}.update", f"Updated {table} record {record_id}")
    await session.commit()
    return serialize_record(row)


@router.post("/admin/sections/{section_key}/records/{record_id}/disable")
async def disable_admin_section(section_key: str, record_id: uuid.UUID, admin: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    table = SECTION_TABLES.get(section_key, section_key.replace("-", "_"))
    if table not in RESOURCE_TABLES:
        raise HTTPException(status_code=404, detail="section_not_found")
    ResourceRepository(session, table, admin.id, roles).ensure_access("update")
    model = MODEL_BY_TABLE.get(table)
    row = await session.get(model, record_id) if model else None
    if row is None:
        raise HTTPException(status_code=404, detail="record_not_found")
    if hasattr(row, "is_active"):
        row.is_active = False
    if hasattr(row, "status"):
        row.status = "disabled"
    _add_audit_log(session, admin.id, f"admin.{table}.disable", f"Disabled {table} record {record_id}")
    await session.commit()
    return serialize_record(row)


@router.get("/marketing/campaigns")
async def campaigns(
    limit: int = 100,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await CampaignService().list(session, roles=roles, limit=limit)


@router.post("/marketing/campaigns", status_code=201)
@router.post("/api/marketing/campaigns", status_code=201)
async def create_campaign(
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await CampaignService().create(session, actor=staff, roles=roles, body=await request.json())}


@router.post("/api/marketing/coupons", status_code=201)
async def api_create_coupon(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if body.get("discount_type") == "percentage" and float(body.get("discount_value") or body.get("amount") or 0) > 100:
        raise HTTPException(status_code=400, detail="invalid_coupon_percentage")
    row = await _api_create(session, "coupons", body, staff)
    await session.commit()
    return {"data": row}


@router.patch("/api/marketing/coupons/{record_id}")
async def api_update_coupon(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _api_update(session, "coupons", record_id, await request.json(), staff)
    await session.commit()
    return {"data": row}


@router.delete("/api/marketing/coupons/{record_id}")
async def api_delete_coupon(record_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    await _api_delete(session, "coupons", record_id)
    await session.commit()
    return {"ok": True}


@router.patch("/api/marketing/campaigns/{record_id}")
async def api_update_campaign(
    record_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await CampaignService().update(session, campaign_id=record_id, actor=staff, roles=roles, body=await request.json())}


@router.post("/api/marketing/campaigns/{record_id}/schedule")
async def api_schedule_campaign(
    record_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await CampaignService().schedule(session, campaign_id=record_id, actor=staff, roles=roles, body=await request.json())}


@router.get("/api/marketing/campaigns/{record_id}/preview")
async def api_preview_campaign(
    record_id: uuid.UUID,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await CampaignService().preview(session, campaign_id=record_id, roles=roles)


@router.post("/api/marketing/campaigns/process-due")
async def api_process_due_campaigns(staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    result = await CampaignService().process_due(session)
    await session.commit()
    return {"data": result}


@router.post("/api/marketing/campaigns/{record_id}/event")
async def api_campaign_event(
    record_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await CampaignService().record_event(session, campaign_id=record_id, actor=staff, roles=roles, body=await request.json())


@router.delete("/api/marketing/campaigns/{record_id}")
async def api_delete_campaign(record_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    await _api_delete(session, "marketing_campaigns", record_id)
    await session.commit()
    return {"ok": True}


@router.post("/api/operations/shipping/carriers", status_code=201)
async def api_create_shipping_carrier(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("shipping_carriers", request, session, staff)


@router.patch("/api/operations/shipping/carriers/{record_id}")
async def api_update_shipping_carrier(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("shipping_carriers", request, session, staff, record_id, "update")


@router.delete("/api/operations/shipping/carriers/{record_id}")
async def api_delete_shipping_carrier(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("shipping_carriers", request, session, staff, record_id, "delete")


@router.post("/api/operations/shipping/stages", status_code=201)
async def api_create_shipping_stage(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("shipping_stages", request, session, staff)


@router.patch("/api/operations/shipping/stages/{record_id}")
async def api_update_shipping_stage(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("shipping_stages", request, session, staff, record_id, "update")


@router.delete("/api/operations/shipping/stages/{record_id}")
async def api_delete_shipping_stage(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("shipping_stages", request, session, staff, record_id, "delete")


@router.get("/couriers")
async def couriers(limit: int = 200, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return [serialize_record(row) for row in await _rows(session, "couriers", limit=limit)]


@router.post("/couriers")
async def create_courier(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    model = MODEL_BY_TABLE["couriers"]
    row = model(name=str(body.get("name") or "مندوب"), phone=body.get("phone"), status=str(body.get("status") or "active"), user_id=_uuid(body["userId"]) if body.get("userId") else None, extra_data=body)
    session.add(row)
    await session.commit()
    return serialize_record(row)


@router.post("/api/admin/orders/manual", status_code=201)
async def api_create_manual_order(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    order = Order(
        order_number=f"E2E-{uuid.uuid4().hex[:10].upper()}",
        user_id=staff.id,
        created_by=staff.id,
        status="pending",
        payment_status="pending",
        payment_method=body.get("payment_method"),
        shipping_total=money(body.get("shipping_cost") or 0),
        notes=body.get("notes"),
        shipping_address=body.get("customer") if isinstance(body.get("customer"), dict) else {},
        extra_data=body,
    )
    session.add(order)
    await session.flush()
    subtotal = Decimal("0")
    for item in body.get("items") or []:
        quantity = int(item.get("quantity") or 1)
        unit_price = money(item.get("unit_price") or item.get("price") or 0)
        total_price = unit_price * quantity
        subtotal += total_price
        session.add(OrderItem(order_id=order.id, product_name=str(item.get("product_name") or "Manual item"), quantity=quantity, unit_price=unit_price, total_price=total_price, extra_data=item))
    order.subtotal = subtotal
    order.total = subtotal + money(order.shipping_total)
    await session.commit()
    return {"data": serialize_record(order)}


@router.patch("/api/admin/orders/{order_id}/assignee")
async def api_assign_order(order_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await session.get(Order, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    body = await request.json()
    row.extra_data = {**(row.extra_data or {}), "assignee_id": body.get("user_id")}
    await session.commit()
    return {"data": serialize_record(row)}


@router.post("/api/admin/orders/{order_id}/invoice-email", status_code=202)
async def api_order_invoice_email(order_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await session.get(Order, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    recipient = await session.get(User, row.user_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="recipient_not_found")
    dedupe_key = f"order-invoice-email:{order_id}:{row.user_id}"
    outbox_model = MODEL_BY_TABLE["email_outbox"]
    existing = (
        await session.execute(
            select(outbox_model)
            .where(
                outbox_model.user_id == row.user_id,
                outbox_model.extra_data["dedupe_key"].astext == dedupe_key,
                outbox_model.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            outbox_model(
                user_id=row.user_id,
                title="Order invoice",
                status="queued",
                email=recipient.email,
                message=f"Order {row.order_number} invoice is ready.",
                extra_data={"order_id": str(order_id), "category": "order", "dedupe_key": dedupe_key, "template": "order_invoice"},
            )
        )
    await session.commit()
    return {"queued": True}


@router.post("/api/orders/{order_id}/communications/resend-confirmation", status_code=202)
async def resend_order_confirmation(order_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user.id or order.deleted_at is not None:
        raise HTTPException(status_code=404, detail="order_not_found")
    dedupe_key = f"order-confirmation-email:{order.id}:{user.id}"
    outbox_model = MODEL_BY_TABLE["email_outbox"]
    existing = (
        await session.execute(
            select(outbox_model)
            .where(
                outbox_model.user_id == user.id,
                outbox_model.extra_data["dedupe_key"].astext == dedupe_key,
                outbox_model.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            outbox_model(
                user_id=user.id,
                title="Order confirmation",
                status="queued",
                email=user.email,
                message=f"Your order {order.order_number} was received.",
                extra_data={"order_id": str(order.id), "category": "order", "dedupe_key": dedupe_key, "template": "order_confirmation"},
            )
        )
    await session.commit()
    return {"queued": True, "status": "queued"}


@router.get("/api/payments/orders/{order_id}")
async def api_list_order_payments(
    order_id: uuid.UUID,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    payment_rows = []
    for table_name in ("order_payments", "payments"):
        payment_model = MODEL_BY_TABLE[table_name]
        result = await session.execute(
            select(payment_model)
            .where(payment_model.order_id == order_id, payment_model.deleted_at.is_(None))
            .order_by(payment_model.created_at.desc())
        )
        payment_rows.extend(result.scalars().all())
    payment_rows.sort(key=lambda row: getattr(row, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {"data": [serialize_record(row) for row in payment_rows]}


@router.post("/api/payments/orders/{order_id}", status_code=201)
async def api_create_order_payment(
    order_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    body = await request.json()
    _validate_payment_record_body(body)
    body["order_id"] = order_id
    body["type"] = body.get("payment_method") or body.get("type") or "cash"
    row = await _api_create(session, "order_payments", body, staff)
    await session.commit()
    return {"data": row}


@router.patch("/api/payments/orders/records/{payment_id}")
async def api_update_order_payment(
    payment_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    body = await request.json()
    _validate_payment_record_body(body)
    row = await _api_update(session, "order_payments", payment_id, body, staff)
    await session.commit()
    return {"data": row}


@router.delete("/api/payments/orders/records/{payment_id}")
async def api_delete_order_payment(
    payment_id: uuid.UUID,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    await _api_delete(session, "order_payments", payment_id)
    await session.commit()
    return {"ok": True}


@router.post("/api/communication/contact", status_code=201)
async def api_create_contact_message(request: Request, session: AsyncSession = Depends(get_session)):
    row = await _api_create(session, "contact_messages", await request.json())
    await session.commit()
    return {"data": row}


@router.patch("/api/admin/contact-messages/{record_id}/read")
async def api_read_contact_message(record_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await session.get(MODEL_BY_TABLE["contact_messages"], record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    row.status = "read"
    await session.commit()
    payload = serialize_record(row)
    payload["is_read"] = True
    return {"data": payload}


@router.delete("/api/admin/contact-messages/{record_id}")
async def api_delete_contact_message(record_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    await _api_delete(session, "contact_messages", record_id)
    await session.commit()
    return {"ok": True}


@router.post("/api/admin/contact-messages/{record_id}/reply", status_code=202)
async def api_reply_contact_message(
    record_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    """Queue an admin reply through the durable email outbox."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="reply_payload_required")
    message_model = MODEL_BY_TABLE["contact_messages"]
    contact = await session.get(message_model, record_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    subject = str(body.get("subject") or "").strip()
    message = str(body.get("message") or "").strip()
    recipient = str(getattr(contact, "email", None) or "").strip()
    if not subject or len(subject) > 300 or "\r" in subject or "\n" in subject:
        raise HTTPException(status_code=422, detail="reply_subject_invalid")
    if not message or len(message) > 20000:
        raise HTTPException(status_code=422, detail="reply_message_invalid")
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", recipient):
        raise HTTPException(status_code=422, detail="contact_email_invalid")
    outbox = await _api_create(session, "email_outbox", {
        "user_id": staff.id,
        "title": subject,
        "status": "queued",
        "email": recipient,
        "message": message,
        "category": "support",
        "consent_required": False,
        "contact_message_id": str(record_id),
        "reply_to": recipient,
    }, staff)
    contact.status = "replied"
    await session.commit()
    return {"queued": True, "data": outbox}


@router.get("/api/support/tickets")
async def api_support_tickets(
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await SupportWorkflowService().list(session, user=user, roles=roles, limit=500)}


@router.get("/api/support/tickets/{ticket_id}")
async def api_support_ticket(
    ticket_id: uuid.UUID,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    row = await SupportWorkflowService().get(session, ticket_id=ticket_id, user=user, roles=roles)
    return {"data": serialize_record(row)}


@router.get("/api/support/tickets/{ticket_id}/messages")
async def api_support_ticket_messages(
    ticket_id: uuid.UUID,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    await SupportWorkflowService().get(session, ticket_id=ticket_id, user=user, roles=roles)
    model = MODEL_BY_TABLE["ticket_messages"]
    result = await session.execute(
        select(model)
        .where(model.ticket_id == ticket_id, model.deleted_at.is_(None))
        .order_by(model.created_at.asc())
        .limit(500)
    )
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.post("/api/support/tickets", status_code=201)
async def api_create_support_ticket(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await SupportWorkflowService().create(session, user=user, roles=roles, body=await request.json())}


@router.post("/api/support/tickets/{ticket_id}/messages", status_code=201)
async def api_create_ticket_message(
    ticket_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await SupportWorkflowService().add_message(session, ticket_id=ticket_id, user=user, roles=roles, body=await request.json())}


@router.patch("/api/support/tickets/{ticket_id}/status")
async def api_update_ticket_status(
    ticket_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return {"data": await SupportWorkflowService().update_status(session, ticket_id=ticket_id, user=staff, roles=roles, body=await request.json())}


@router.delete("/api/support/tickets/{ticket_id}")
async def api_delete_support_ticket(
    ticket_id: uuid.UUID,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await SupportWorkflowService().delete(session, ticket_id=ticket_id, user=staff, roles=roles)


@router.post("/api/finance/employee-payments", status_code=201)
async def api_create_employee_payment(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("employee_payments", request, session, staff)


@router.patch("/api/finance/employee-payments/{record_id}/status")
async def api_update_employee_payment_status(record_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("employee_payments", request, session, staff, record_id, "update")


@router.post("/api/finance/expenses", status_code=201)
async def api_create_expense(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="expense_payload_required")

    raw_amount = body.get("amount")
    try:
        amount = Decimal(str(raw_amount).strip())
    except (ArithmeticError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="expense_amount_invalid")
    if not amount.is_finite() or amount <= 0:
        raise HTTPException(status_code=422, detail="expense_amount_invalid")

    description = str(body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=422, detail="expense_description_required")

    category = str(
        body.get("expense_category") or body.get("category") or body.get("type") or "other"
    ).strip()[:64] or "other"
    payload = {
        "type": category,
        "amount": amount,
        "description": description,
        "status": "pending",
        "expense_category": category,
        "currency_code": str(body.get("currency_code") or "YER").strip().upper()[:12],
        "expense_date": str(body.get("expense_date") or datetime.now(timezone.utc).isoformat()),
        "vendor_name": str(body.get("vendor_name") or "").strip() or None,
        "vendor_phone": str(body.get("vendor_phone") or "").strip() or None,
        "notes": str(body.get("notes") or "").strip() or None,
        "receipt_url": str(body.get("receipt_url") or "").strip() or None,
        "is_recurring": bool(body.get("is_recurring", False)),
        "recurring_period": str(body.get("recurring_period") or "").strip() or None,
        "voucher_number": str(body.get("voucher_number") or f"EXP-{uuid.uuid4().hex[:10].upper()}"),
    }
    row = await _api_create(session, "general_expenses", payload, staff)
    await session.commit()
    return {"data": _general_expense_payload(row)}


@router.post("/api/finance/cash-transactions", status_code=201)
async def api_create_cash_transaction(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("cash_transactions", request, session, staff)


@router.post("/api/finance/vouchers", status_code=201)
async def api_create_financial_voucher(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="invalid_voucher_payload")
    raw_type = str(body.get("voucher_type") or body.get("type") or "").strip().lower()
    if raw_type not in {"receipt", "payment"}:
        raise HTTPException(status_code=422, detail="invalid_voucher_type")
    try:
        amount = Decimal(str(body.get("amount")))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise HTTPException(status_code=422, detail="voucher_amount_invalid") from exc
    if amount <= 0:
        raise HTTPException(status_code=422, detail="voucher_amount_must_be_positive")
    beneficiary_name = str(body.get("beneficiary_name") or body.get("beneficiaryName") or "").strip()
    if not beneficiary_name:
        raise HTTPException(status_code=422, detail="voucher_beneficiary_required")
    payment_method = str(body.get("payment_method") or body.get("paymentMethod") or "cash").strip().lower()
    if not payment_method:
        raise HTTPException(status_code=422, detail="voucher_payment_method_required")
    payload = {
        **body,
        "voucher_type": raw_type,
        "type": raw_type,
        "amount": amount,
        "currency_code": str(body.get("currency_code") or "YER").strip().upper(),
        "beneficiary_name": beneficiary_name,
        "payment_method": payment_method,
        "issued_date": body.get("issued_date") or datetime.now(timezone.utc).isoformat(),
        "voucher_number": body.get("voucher_number") or f"{'RV' if raw_type == 'receipt' else 'PV'}-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}",
        "status": str(body.get("status") or "pending").strip().lower(),
    }
    row = await _api_create(session, "financial_vouchers", payload, staff)
    await session.commit()
    return {"data": _financial_voucher_payload(row)}


@router.post("/api/dashboard/risk-alerts", status_code=201)
async def api_create_risk_alert(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return await _create_update_delete_resource("risk_alerts", request, session, staff)


@router.get("/api/dashboard/risk-alerts")
async def api_list_risk_alerts(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": []}


@router.patch("/api/dashboard/risk-alerts/{record_id}/acknowledge")
async def api_ack_risk_alert(record_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await session.get(MODEL_BY_TABLE["risk_alerts"], record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="risk_alert_not_found")
    row.is_acknowledged = True
    await session.commit()
    return {"data": serialize_record(row)}


@router.get("/api/admin-data/export/products")
async def api_export_products(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    return {"data": [serialize_record(row) for row in await _rows(session, "products", limit=5000)]}


ADMIN_EXPORT_DATASETS = {
    "orders": "orders",
    "customers": "profiles",
    "categories": "categories",
    "brands": "brands",
    "partners": "user_roles",
}


# These are the real sections rendered by the public home page.  They are
# persisted only when an administrator opens the sections control, so an
# empty page_sections table does not make the design screen appear broken.
# Existing rows are preserved and only missing system keys are added.
DEFAULT_HOME_PAGE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("hero", "البانر الرئيسي"),
    ("flash_deals", "العروض السريعة"),
    ("featured_products", "المنتجات المميزة"),
    ("trust_guarantees", "ضمانات التسوق"),
    ("international", "التسوق الدولي"),
    ("local_shopping", "التسوق المحلي"),
    ("partners", "شركاؤنا"),
    ("loyalty_partner", "برنامج الولاء"),
    ("store_reviews", "آراء العملاء"),
    ("services", "خدماتنا"),
)


async def _ensure_home_page_sections(session: AsyncSession) -> None:
    """Materialize missing home-page controls without creating demo content."""
    model = MODEL_BY_TABLE["page_sections"]
    rows = await _rows(session, "page_sections", limit=500)
    existing_keys = {
        str((row.extra_data or {}).get("section_key") or "").strip()
        for row in rows
        if str((row.extra_data or {}).get("page") or "").strip() == "home"
    }
    missing = [item for item in DEFAULT_HOME_PAGE_SECTIONS if item[0] not in existing_keys]
    if not missing:
        return
    current_max = max(
        [int(getattr(row, "sort_order", 0) or 0) for row in rows if str((row.extra_data or {}).get("page") or "").strip() == "home"]
        or [0]
    )
    for offset, (section_key, section_name) in enumerate(missing, start=1):
        session.add(
            model(
                title=section_name,
                status="active",
                sort_order=current_max + offset,
                is_active=True,
                extra_data={
                    "page": "home",
                    "section_key": section_key,
                    "section_name": section_name,
                    "is_visible": True,
                    "settings": {},
                    "source": "system_default",
                },
            )
        )
    await session.commit()


@router.get("/api/admin-data/export/{dataset}")
async def api_export_admin_dataset(dataset: str, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    """Export the datasets exposed by the admin data screen.

    The frontend has always advertised these exports, but the backend only
    implemented the products route. Keep the response shape stable and use
    the same server-side serializers as the admin screens.
    """
    table = ADMIN_EXPORT_DATASETS.get(str(dataset or "").strip().lower())
    if table is None:
        raise HTTPException(status_code=404, detail="admin_export_dataset_not_found")
    if table == "orders":
        rows = await _rows(session, table, limit=5000)
        return {"data": await _serialize_orders_with_financials(session, rows)}
    if table == "profiles":
        result = await session.execute(
            select(Profile)
            .join(UserRole, UserRole.user_id == Profile.user_id)
            .where(Profile.deleted_at.is_(None), UserRole.role == "customer")
            .order_by(Profile.created_at.desc())
            .limit(5000)
        )
        return {"data": [serialize_record(row) for row in result.scalars()]}
    if table == "user_roles":
        rows = await _rows(session, table, limit=5000)
        return {"data": [serialize_record(row) for row in rows if str(getattr(row, "role", "") or "").lower() == "partner"]}
    return {"data": [serialize_record(row) for row in await _rows(session, table, limit=5000)]}


@router.post("/api/admin-data/backup", status_code=201)
async def api_reject_or_create_backup(request: Request, staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    tables = [str(item) for item in body.get("tables") or []]
    coordinator = BackupCoordinator()
    row = (
        await coordinator.create_backup(session, actor=staff, selected_tables=tables)
        if get_settings().app_env == "test"
        else await coordinator.queue_backup(session, actor=staff, selected_tables=tables)
    )
    await session.commit()
    return {"data": {**row, "url": row.get("download_url")}}


@router.get("/api/admin-data/backup/{backup_id}")
async def api_download_backup(backup_id: uuid.UUID, staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await BackupCoordinator().download(session, backup_id)


@router.get("/delivery/assignments")
async def delivery_assignments(user: User = Depends(require_courier), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["courier_assignments"]
    result = await session.execute(select(model).where(or_(model.user_id == user.id, model.courier_id == user.id)).order_by(model.created_at.desc()))
    return [serialize_record(row) for row in result.scalars()]


@router.patch("/delivery/assignments/{assignment_id}/status")
async def delivery_assignment_status(assignment_id: uuid.UUID, request: Request, user: User = Depends(require_courier), session: AsyncSession = Depends(get_session)):
    return await CourierLocationService().update_status(session, user=user, assignment_id=assignment_id, body=await request.json())


@router.post("/delivery/location")
async def delivery_location(request: Request, user: User = Depends(require_courier), session: AsyncSession = Depends(get_session)):
    return await CourierLocationService().record(session, user=user, body=await request.json())


@router.get("/marketer/dashboard")
async def marketer_dashboard(user: User = Depends(require_marketer), session: AsyncSession = Depends(get_session)):
    marketer_model = MODEL_BY_TABLE["marketers"]
    campaign_model = MODEL_BY_TABLE["marketing_campaigns"]
    commissions_model = MODEL_BY_TABLE["marketer_commissions"]
    payments_model = MODEL_BY_TABLE["marketer_payments"]
    profile = (
        await session.execute(
            select(marketer_model)
            .where(marketer_model.user_id == user.id, marketer_model.deleted_at.is_(None))
            .order_by(marketer_model.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    campaigns = (
        await session.execute(
            select(campaign_model)
            .where(campaign_model.deleted_at.is_(None), campaign_model.status.in_(["active", "published"]))
            .order_by(campaign_model.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    commissions = (
        await session.execute(
            select(commissions_model)
            .where(commissions_model.user_id == user.id, commissions_model.deleted_at.is_(None))
            .order_by(commissions_model.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    payments = (
        await session.execute(
            select(payments_model)
            .where(payments_model.user_id == user.id, payments_model.deleted_at.is_(None))
            .order_by(payments_model.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    pending = sum(money(getattr(row, "amount", 0)) for row in commissions if getattr(row, "status", "") in {"pending", "earned", "approved"})
    paid = sum(money(getattr(row, "amount", 0)) for row in payments if getattr(row, "status", "") in {"paid", "completed", "approved"})
    return {
        "profile": serialize_record(profile) if profile is not None else {"user_id": str(user.id)},
        "campaigns": [serialize_record(row) for row in campaigns],
        "commissions": [serialize_record(row) for row in commissions],
        "payments": [serialize_record(row) for row in payments],
        "pendingAmount": str(pending),
        "paidAmount": str(paid),
    }


def _page_window(page: int, page_size: int | None, limit: int | None, *, default_page_size: int, max_page_size: int) -> tuple[int, int, int]:
    resolved_page = max(int(page or 1), 1)
    requested_size = page_size if page_size is not None else limit
    resolved_size = min(max(int(requested_size or default_page_size), 1), max_page_size)
    return resolved_page, resolved_size, (resolved_page - 1) * resolved_size


def _notification_visible_clause(model: Any, user_id: uuid.UUID) -> Any:
    clauses = [
        or_(model.user_id == user_id, model.recipient_id == user_id),
        model.deleted_at.is_(None),
    ]
    if "expires_at" in model.__table__.c:
        clauses.append(or_(model.expires_at.is_(None), model.expires_at > datetime.now(timezone.utc)))
    return and_(*clauses)


def _paginated_payload(items: list[dict[str, Any]], total: int, page: int, page_size: int, *, unread: int | None = None, queue: dict[str, Any] | None = None) -> dict[str, Any]:
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    payload: dict[str, Any] = {
        "items": items,
        "data": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
    }
    if unread is not None:
        payload["unread"] = unread
        payload["unread_count"] = unread
    if queue is not None:
        payload["queue"] = queue
    return payload


async def _notification_queue_summary(session: AsyncSession, user_id: uuid.UUID | None = None) -> dict[str, int]:
    model = MODEL_BY_TABLE["notification_outbox"]
    clauses = [model.deleted_at.is_(None)]
    if user_id is not None:
        clauses.append(model.user_id == user_id)
    rows = (
        await session.execute(
            select(model.status, func.count())
            .where(and_(*clauses))
            .group_by(model.status)
        )
    ).all()
    summary = {
        "queued": 0,
        "pending": 0,
        "processing": 0,
        "processed": 0,
        "failed_retryable": 0,
        "dead_letter": 0,
        "blocked_configuration": 0,
    }
    for status, count in rows:
        key = str(status or "unknown")
        summary[key] = int(count or 0)
    summary["visible_backlog"] = summary.get("queued", 0) + summary.get("pending", 0) + summary.get("failed_retryable", 0)
    return summary


@router.get("/notifications")
@router.get("/api/notifications")
async def notifications(
    request: Request,
    limit: int | None = None,
    page: int = 1,
    page_size: int | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["notifications"]
    try:
        resolved_page, resolved_page_size, offset = _page_window(
            page,
            page_size,
            limit,
            default_page_size=100,
            max_page_size=500,
        )
        where_clause = _notification_visible_clause(model, user.id)
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(where_clause)
                )
            ).scalar_one()
            or 0
        )
        result = await session.execute(
            select(model)
            .where(where_clause)
            .order_by(model.created_at.desc())
            .offset(offset)
            .limit(resolved_page_size)
        )
        items = [serialize_record(row) for row in result.scalars()]
        if request.url.path == "/notifications":
            return items
        unread = await NotificationService(session).get_unread_count(user.id)
        queue = await _notification_queue_summary(session, user.id)
        return _paginated_payload(items, total, resolved_page, resolved_page_size, unread=unread, queue=queue)
    except Exception:
        await session.rollback()
        if request.url.path == "/notifications":
            return []
        return _paginated_payload([], 0, 1, page_size or limit or 100, unread=0, queue={"queued": 0, "failed_retryable": 0, "dead_letter": 0, "processing": 0})


@router.get("/notifications/unread-count")
@router.get("/api/notifications/unread-count")
async def unread_notifications_count(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    try:
        unread = await NotificationService(session).get_unread_count(user.id)
    except Exception:
        await session.rollback()
        unread = 0
    return {"unread": unread}


@router.patch("/notifications/{notification_id}/read")
@router.patch("/api/notifications/{notification_id}/read")
async def read_notification(notification_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    row = await NotificationService(session).mark_notification_read(notification_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="notification_not_found")
    await session.commit()
    return serialize_record(row)


@router.patch("/notifications/read-all")
@router.post("/api/notifications/read-all")
@router.patch("/api/notifications/read-all")
async def read_all_notifications(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    updated = await NotificationService(session).mark_all_notifications_read(user.id)
    await session.commit()
    return {"ok": True, "updated": updated}


@router.delete("/notifications/{notification_id}")
@router.delete("/api/notifications/{notification_id}")
async def delete_user_notification(notification_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["notifications"]
    row = (
        await session.execute(
            select(model)
            .where(model.id == notification_id, _notification_visible_clause(model, user.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="notification_not_found")
    row.deleted_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


async def _admin_notifications_table_ready(session: AsyncSession) -> bool:
    try:
        result = await session.execute(select(func.to_regclass("public.admin_notifications")))
        return result.scalar_one_or_none() is not None
    except Exception:
        await session.rollback()
        return False


def _admin_notification_scope(model: Any, staff: User) -> Any:
    return and_(
        model.deleted_at.is_(None),
        or_(model.recipient_id == staff.id, model.user_id == staff.id),
    )


async def _get_scoped_admin_notification(session: AsyncSession, notification_id: uuid.UUID, staff: User) -> Any:
    model = MODEL_BY_TABLE["admin_notifications"]
    row = (
        await session.execute(
            select(model)
            .where(model.id == notification_id, _admin_notification_scope(model, staff))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="notification_recipient_mismatch")
    return row


@router.get("/admin-notifications")
async def admin_notifications_legacy(limit: int = 500, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    if not await _admin_notifications_table_ready(session):
        return []
    model = MODEL_BY_TABLE["admin_notifications"]
    try:
        result = await session.execute(
            select(model)
            .where(_admin_notification_scope(model, staff))
            .order_by(model.created_at.desc())
            .limit(min(limit, 500))
        )
        return [serialize_record(row) for row in result.scalars()]
    except Exception:
        await session.rollback()
        return []


@router.get("/api/notifications/admin")
async def admin_notifications_api(
    limit: int | None = None,
    page: int = 1,
    page_size: int | None = None,
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    if not await _admin_notifications_table_ready(session):
        return _paginated_payload([], 0, 1, page_size or limit or 100, unread=0, queue={"queued": 0, "failed_retryable": 0, "dead_letter": 0, "processing": 0})
    model = MODEL_BY_TABLE["admin_notifications"]
    try:
        resolved_page, resolved_page_size, offset = _page_window(
            page,
            page_size,
            limit,
            default_page_size=100,
            max_page_size=500,
        )
        scope = _admin_notification_scope(model, staff)
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(scope)
                )
            ).scalar_one()
            or 0
        )
        unread = int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(scope, model.is_read.is_(False))
                )
            ).scalar_one()
            or 0
        )
        result = await session.execute(
            select(model)
            .where(scope)
            .order_by(model.created_at.desc())
            .offset(offset)
            .limit(resolved_page_size)
        )
        rows = [serialize_record(row) for row in result.scalars()]
        return _paginated_payload(rows, total, resolved_page, resolved_page_size, unread=unread, queue=await _notification_queue_summary(session))
    except Exception:
        await session.rollback()
        return _paginated_payload([], 0, 1, page_size or limit or 100, unread=0, queue={"queued": 0, "failed_retryable": 0, "dead_letter": 0, "processing": 0})


@router.patch("/admin-notifications/{notification_id}/read")
@router.patch("/api/notifications/admin/{notification_id}/read")
async def read_admin_notification(notification_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _get_scoped_admin_notification(session, notification_id, staff)
    try:
        body = await request.json()
    except Exception:
        body = {}
    read = bool(body.get("isRead", body.get("is_read", True)))
    row.is_read = read
    row.read_at = datetime.now(timezone.utc) if read else None
    await _record_and_publish_realtime(
        session,
        channel=f"user:{staff.id}",
        event="admin.notification.read",
        payload={"id": str(row.id), "is_read": read},
        dedupe_key=f"admin.notification.read:{row.id}:{row.read_at.isoformat() if row.read_at else 'unread'}",
        user_id=staff.id,
    )
    await session.commit()
    return serialize_record(row)


@router.patch("/admin-notifications/read-all")
@router.post("/api/notifications/admin/read-all")
@router.patch("/api/notifications/admin/read-all")
async def read_all_admin_notifications(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["admin_notifications"]
    rows = (
        await session.execute(
            select(model).where(_admin_notification_scope(model, staff), model.is_read.is_(False)).limit(5000)
        )
    ).scalars().all()
    for row in rows:
        row.is_read = True
        row.read_at = datetime.now(timezone.utc)
    await _record_and_publish_realtime(
        session,
        channel=f"user:{staff.id}",
        event="admin.notifications.read_all",
        payload={"updated": len(rows)},
        dedupe_key=f"admin.notifications.read_all:{staff.id}:{datetime.now(timezone.utc).isoformat()}",
        user_id=staff.id,
    )
    await session.commit()
    return {"ok": True, "updated": len(rows)}


@router.delete("/admin-notifications/{notification_id}")
@router.delete("/api/notifications/admin/{notification_id}")
async def delete_admin_notification(notification_id: uuid.UUID, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _get_scoped_admin_notification(session, notification_id, staff)
    row.deleted_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


@router.post("/admin-notifications/bulk-delete")
@router.post("/api/notifications/admin/bulk-delete")
async def bulk_delete_admin_notifications(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    ids = [_uuid(item) for item in body.get("ids", [])]
    model = MODEL_BY_TABLE["admin_notifications"]
    scoped_rows = (
        await session.execute(
            select(model).where(model.id.in_(ids), _admin_notification_scope(model, staff)).with_for_update()
        )
    ).scalars().all()
    for row in scoped_rows:
        row.deleted_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True, "deleted": len(scoped_rows)}


@router.patch("/admin-notifications/{notification_id}/type")
@router.patch("/api/notifications/admin/{notification_id}/type")
async def update_admin_notification_type(notification_id: uuid.UUID, request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    row = await _get_scoped_admin_notification(session, notification_id, staff)
    body = await request.json()
    row.type = str(body.get("type") or row.type)
    await session.commit()
    return serialize_record(row)


async def _send_notifications(session: AsyncSession, body: dict[str, Any], actor: User, roles: set[str]) -> int:
    if not roles.intersection(ADMIN_NOTIFICATION_ROLES):
        raise HTTPException(status_code=403, detail="communication_permission_denied")
    recipients = body.get("userIds") or body.get("user_ids") or body.get("recipients") or []
    audience = str(body.get("audience") or "").strip()
    confirm_audience = bool(body.get("confirmAudience") or body.get("confirm_audience"))
    preview_confirmed = bool(body.get("previewConfirmed") or body.get("preview_confirmed"))
    if not recipients:
        if audience != "all_active_users":
            raise HTTPException(status_code=422, detail="recipients_required")
        if not (confirm_audience and preview_confirmed):
            raise HTTPException(status_code=409, detail="audience_confirmation_required")
        result = await session.execute(select(User.id).where(User.is_active.is_(True), User.deleted_at.is_(None)))
        recipients = list(result.scalars())
    limit = get_settings().message_bulk_recipient_limit
    if len(recipients) > limit:
        raise HTTPException(status_code=413, detail="recipient_limit_exceeded")
    if not str(body.get("title") or "").strip() or not str(body.get("message") or body.get("body") or "").strip():
        raise HTTPException(status_code=422, detail="template_not_allowed")
    service = NotificationService(session)
    count = 0
    seen: set[uuid.UUID] = set()
    for raw_user_id in recipients:
        user_id = _uuid(raw_user_id)
        if user_id in seen:
            continue
        seen.add(user_id)
        await service.create_notification(NotificationPayload(
            user_id=user_id,
            title=str(body.get("title") or "Notification"),
            body=str(body.get("message") or body.get("body") or ""),
            notification_type=str(body.get("type") or body.get("notification_type") or "message"),
            category=str(body.get("category") or "system"),
            priority=str(body.get("priority") or "normal"),
            action_url=body.get("actionUrl") or body.get("url") or body.get("deep_link"),
            entity_type=body.get("entityType") or body.get("entity_type"),
            entity_id=str(body.get("entityId") or body.get("entity_id") or "") or None,
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {"audience": audience or "explicit_recipients"},
            created_by=actor.id,
            source="admin",
            deduplication_key=body.get("deduplicationKey") or body.get("deduplication_key") or f"admin-broadcast:{actor.id}:{user_id}:{hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:24]}",
        ))
        count += 1
    await session.flush()
    return count


@router.post("/notifications/send")
@router.post("/notifications/fanout")
@router.post("/api/notifications/admin/broadcast", status_code=201)
async def send_notifications(request: Request, staff: User = Depends(require_staff), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    count = await _send_notifications(session, await request.json(), staff, roles)
    await session.commit()
    return {"ok": True, "sent": count, "count": count}


@router.post("/admin-notifications/send")
@router.post("/api/notifications/admin/log", status_code=201)
async def send_admin_notification(
    request: Request,
    response: Response,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    raw_recipients = body.get("staffIds") or body.get("staff_ids") or body.get("recipients")
    if raw_recipients is not None:
        if not roles.intersection(ADMIN_NOTIFICATION_ROLES):
            raise HTTPException(status_code=403, detail="communication_permission_denied")
        if not raw_recipients:
            raise HTTPException(status_code=422, detail="recipients_required")
        recipients = [_uuid(item) for item in raw_recipients]
    else:
        recipients = [staff.id]
    model = MODEL_BY_TABLE["admin_notifications"]
    created = []
    for recipient_id in dict.fromkeys(recipients):
        row = model(
            user_id=recipient_id,
            recipient_id=recipient_id,
            title=str(body.get("title") or "Admin notification"),
            body=str(body.get("message") or body.get("body") or ""),
            message=str(body.get("message") or body.get("body") or ""),
            type=str(body.get("type") or "message"),
            notification_type=str(body.get("type") or "message"),
            category=str(body.get("category") or "system"),
            priority=str(body.get("priority") or "normal"),
            status="new",
            is_read=False,
            created_by=staff.id,
            source="admin",
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
            extra_data={"created_by_staff": str(staff.id), "recipient_id": str(recipient_id)},
        )
        session.add(row)
        await session.flush()
        created.append(row)
        await _record_and_publish_realtime(
            session,
            channel=f"user:{recipient_id}",
            event="admin.notification.created",
            payload=serialize_record(row),
            dedupe_key=f"admin.notification.created:{row.id}",
            user_id=recipient_id,
        )
    await session.commit()
    payload = serialize_record(created[0]) if len(created) == 1 else [serialize_record(row) for row in created]
    if request.url.path == "/admin-notifications/send":
        response.status_code = 200
        return payload
    return {"data": payload}


@router.post("/notifications/device-token")
@router.post("/api/notifications/push-token")
async def register_notification_device(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    try:
        result = await NotificationService(session).register_device_token(user.id, await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await session.commit()
    return {"ok": True, "device": result}


@router.delete("/notifications/device-token")
@router.post("/notifications/device-token/unregister")
async def unregister_notification_device(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    token = str(body.get("token") or body.get("deviceToken") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token_required")
    removed = await NotificationService(session).unregister_device_token(user.id, token)
    await session.commit()
    return {"ok": True, "removed": removed}


@router.post("/notifications/web-push-subscription")
@router.post("/api/notifications/web-push-subscription")
async def register_web_push_subscription(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    try:
        result = await NotificationService(session).register_web_push_subscription(user.id, await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    settings = get_settings()
    await session.commit()
    return {"ok": True, "subscription": result, "vapidConfigured": bool(settings.vapid_public_key and settings.vapid_private_key)}


@router.get("/notifications/preferences")
@router.get("/api/notifications/preferences")
async def get_notification_preferences(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    row = await NotificationService(session).preferences_for(user.id)
    await session.commit()
    return serialize_record(row)


@router.put("/notifications/preferences")
@router.patch("/notifications/preferences")
@router.put("/api/notifications/preferences")
@router.patch("/api/notifications/preferences")
async def save_notification_preferences(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    result = await NotificationService(session).update_preferences(user.id, await request.json())
    await session.commit()
    return result


@router.get("/notifications/outbox")
@router.get("/api/notifications/outbox")
async def notification_outbox(
    request: Request,
    limit: int | None = None,
    page: int = 1,
    page_size: int | None = None,
    status: str | None = None,
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["notification_outbox"]
    resolved_page, resolved_page_size, offset = _page_window(
        page,
        page_size,
        limit,
        default_page_size=100,
        max_page_size=500,
    )
    clauses = [model.deleted_at.is_(None)]
    if status:
        clauses.append(model.status == status)
    where_clause = and_(*clauses)
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(model).where(where_clause)
            )
        ).scalar_one()
        or 0
    )
    rows = (
        await session.execute(
            select(model)
            .where(where_clause)
            .order_by(model.created_at.desc())
            .offset(offset)
            .limit(resolved_page_size)
        )
    ).scalars().all()
    items = [serialize_record(row) for row in rows]
    if request.url.path == "/notifications/outbox":
        return items
    return _paginated_payload(items, total, resolved_page, resolved_page_size, queue=await _notification_queue_summary(session))


@router.get("/api/internal/messaging/health")
async def messaging_worker_health(admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    metrics: dict[str, Any] = {}
    for table_name in ("notification_outbox", "email_outbox", "whatsapp_outbox"):
        model = MODEL_BY_TABLE[table_name]
        rows = (
            await session.execute(
                select(model.status, func.count())
                .where(model.deleted_at.is_(None))
                .group_by(model.status)
            )
        ).all()
        metrics[table_name] = {str(status or "unknown"): int(count) for status, count in rows}
    heartbeat_model = MODEL_BY_TABLE["operational_alerts"]
    heartbeat = (
        await session.execute(
            select(heartbeat_model)
            .where(
                heartbeat_model.type == "message_worker_heartbeat",
                heartbeat_model.status == "active",
                heartbeat_model.deleted_at.is_(None),
            )
            .order_by(heartbeat_model.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "status": "ok",
        "queues": metrics,
        "worker_last_heartbeat": serialize_record(heartbeat).get("updated_at") if heartbeat is not None else None,
        "worker_last_heartbeat_data": heartbeat.extra_data if heartbeat is not None and isinstance(heartbeat.extra_data, dict) else {},
    }


@router.get("/api/internal/messaging/device-status")
async def messaging_device_status(admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Return non-secret delivery registration diagnostics for the admin user."""
    token_model = MODEL_BY_TABLE["push_tokens"]
    attempt_model = MODEL_BY_TABLE["notification_delivery_attempts"]
    rows = (
        await session.execute(
            select(token_model)
            .where(
                token_model.user_id == admin.id,
                token_model.deleted_at.is_(None),
            )
            .order_by(token_model.updated_at.desc())
            .limit(20)
        )
    ).scalars().all()
    attempts = (
        await session.execute(
            select(attempt_model)
            .where(
                attempt_model.user_id == admin.id,
                attempt_model.deleted_at.is_(None),
            )
            .order_by(attempt_model.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return {
        "status": "ok",
        "user_id": str(admin.id),
        "firebase": firebase_admin_configuration_status(),
        "tokens": [
            {
                "platform": row.platform,
                "environment": row.environment,
                "status": row.status,
                "is_active": bool(row.is_active),
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                "app_version": row.app_version,
            }
            for row in rows
        ],
        "recent_delivery_attempts": [
            {
                "notification_id": str(row.notification_id) if row.notification_id else None,
                "channel": row.channel,
                "provider": row.provider,
                "status": row.status,
                "response_code": row.response_code,
                "error_code": row.error_code,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in attempts
        ],
    }


@router.post("/notifications/outbox/process")
async def process_notification_outbox(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    raise HTTPException(status_code=410, detail="manual_worker_invocation_disabled")

@router.get("/email/outbox")
@router.get("/whatsapp/outbox")
async def outbox(request: Request, limit: int = 500, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    table = "notification_outbox" if request.url.path.startswith("/notifications") else "email_outbox" if request.url.path.startswith("/email") else "whatsapp_outbox"
    return [serialize_record(row) for row in await _rows(session, table, limit=limit)]


@router.post("/email/send")
@router.post("/whatsapp/send")
async def queue_external_message(request: Request, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    raise HTTPException(status_code=410, detail="typed_communication_endpoint_required")


@router.post("/email/process")
async def process_email_queue(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    raise HTTPException(status_code=410, detail="manual_worker_invocation_disabled")


@router.post("/whatsapp/process")
async def process_whatsapp_queue(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    raise HTTPException(status_code=410, detail="manual_worker_invocation_disabled")


@router.post("/support-tickets", status_code=201, operation_id="create_support_ticket_legacy")
@router.post("/support/tickets", status_code=201, operation_id="create_support_ticket")
async def create_support_ticket(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await SupportWorkflowService().create(session, user=user, roles=roles, body=await request.json())


@router.get("/reports/summary")
@router.get("/admin/reports/summary")
@router.get("/partner/reports/summary")
async def reports_summary(request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    statement = select(Order).where(Order.deleted_at.is_(None))
    if request.url.path.startswith("/partner"):
        if "partner" not in roles:
            raise HTTPException(status_code=403, detail="partner_required")
        order_join = OrderItem.__table__.join(Order.__table__, OrderItem.order_id == Order.id)
        base_clauses = (Order.deleted_at.is_(None), OrderItem.partner_id == user.id)
        orders_count = int(
            (
                await session.execute(
                    select(func.count(func.distinct(Order.id))).select_from(order_join).where(*base_clauses)
                )
            ).scalar_one()
            or 0
        )
        revenue = money(
            (
                await session.execute(
                    select(func.coalesce(func.sum(OrderItem.total_price), 0)).select_from(order_join).where(*base_clauses)
                )
            ).scalar_one()
            or 0
        )
        pending = int(
            (
                await session.execute(
                    select(func.count(func.distinct(Order.id)))
                    .select_from(order_join)
                    .where(*base_clauses, Order.status.in_(["pending", "processing"]))
                )
            ).scalar_one()
            or 0
        )
        completed = int(
            (
                await session.execute(
                    select(func.count(func.distinct(Order.id)))
                    .select_from(order_join)
                    .where(*base_clauses, Order.status == "delivered")
                )
            ).scalar_one()
            or 0
        )
        products_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Product).where(Product.deleted_at.is_(None), Product.partner_id == user.id)
                )
            ).scalar_one()
            or 0
        )
        recognized = await RevenueRecognitionService.summary(session, partner_id=user.id)
        revenue_text = str(recognized["net_revenue"])
        return {
            "orders": orders_count,
            "ordersCount": orders_count,
            "revenue": revenue_text,
            "totalRevenue": revenue_text,
            "merchantRevenue": revenue_text,
            "pending": pending,
            "completed": completed,
            "productsCount": products_count,
            "appPending": 0,
            "aggregation": "own_order_items_successful_payments_minus_refunds",
            "recognizedRevenue": recognized,
        }
    elif request.url.path.startswith("/admin") and not roles.intersection({"admin", "manager", "finance", "staff"}):
        raise HTTPException(status_code=403, detail="staff_required")
    elif not request.url.path.startswith("/admin"):
        statement = statement.where(Order.user_id == user.id)
    orders = list((await session.execute(statement)).scalars())
    recognized = await RevenueRecognitionService.summary(session)
    return {"orders": len(orders), "revenue": str(recognized["net_revenue"]), "recognizedRevenue": recognized, "pending": sum(1 for row in orders if row.status in {"pending", "processing"}), "completed": sum(1 for row in orders if row.status == "delivered")}


@router.get("/reports/sales")
@router.get("/reports/orders")
@router.get("/reports/products")
async def report_rows(request: Request, limit: int = 500, staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    table = "products" if request.url.path.endswith("products") else "orders"
    return [serialize_record(row) for row in await _rows(session, table, limit=limit)]


@router.post("/reports/export")
async def export_report(
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await ReportGenerationService(session).generate_export(
        request,
        actor=staff,
        roles=roles,
        body=await request.json(),
        idempotency_key=idempotency_key,
    )


@router.get("/reports/exports")
async def report_exports(
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await ReportGenerationService(session).list_exports(request, actor=staff, roles=roles, limit=500)


@router.get("/reports/exports/{export_id}/download")
async def download_report_export(
    export_id: uuid.UUID,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await ReportGenerationService(session).download(export_id, actor=staff, roles=roles)


@router.post("/shipping/quote")
async def shipping_quote(request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.json()
    zones_model = MODEL_BY_TABLE["shipping_zones"]
    zone_id = body.get("shippingZoneId") or body.get("shipping_zone_id")
    statement = select(zones_model).where(zones_model.is_active.is_(True), zones_model.deleted_at.is_(None))
    if zone_id:
        statement = statement.where(zones_model.id == _uuid(zone_id, "shippingZoneId"))
    result = await session.execute(statement.order_by(zones_model.sort_order))
    zones = list(result.scalars())
    selected = zones[0] if zone_id and zones else None
    if selected is None:
        destination = str(
            body.get("city")
            or body.get("governorate")
            or body.get("address")
            or ""
        ).strip().lower()
        for row in zones:
            data = serialize_record(row)
            candidates = [
                str(data.get("city") or ""),
                str(data.get("governorate") or ""),
                str(data.get("name") or ""),
                str(data.get("name_en") or ""),
            ]
            if any(candidate and candidate.strip().lower() in destination for candidate in candidates):
                selected = row
                break
    if selected is None:
        raise HTTPException(status_code=404, detail="shipping_zone_not_found")
    fee = str(money(selected.fee or 0))
    zone = serialize_record(selected)
    label = zone.get("name") or zone.get("city") or zone.get("governorate") or "shipping"
    return {
        "shippingCost": fee,
        "fee": fee,
        "currencyCode": "YER",
        "zoneId": str(selected.id),
        "shippingZoneId": str(selected.id),
        "matchedZone": label,
        "label": f"الشحن إلى {label}",
        "isEstimated": False,
        "zone": zone,
    }


@router.post("/coupons/validate")
async def validate_coupon(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    code = str(body.get("code") or "").upper().strip()
    model = MODEL_BY_TABLE["coupons"]
    result = await session.execute(select(model).where(func.upper(model.code) == code, model.is_active.is_(True)).limit(1))
    coupon = result.scalar_one_or_none()
    if coupon is None or (coupon.expires_at and coupon.expires_at < datetime.now(timezone.utc)):
        raise HTTPException(status_code=404, detail="coupon_invalid")
    return {**serialize_record(coupon), "valid": True, "discountAmount": str(money(coupon.amount or 0))}


@router.post("/loyalty/initialize", status_code=201)
async def initialize_loyalty(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["user_loyalty"]
    result = await session.execute(select(model).where(model.user_id == user.id).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        row = model(user_id=user.id, status="active", balance=Decimal("0.00"), extra_data={"joined": True})
        session.add(row)
        await session.commit()
    return {"data": serialize_record(row)}


@router.get("/loyalty/me")
async def loyalty_me(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["user_loyalty"]
    result = await session.execute(select(model).where(model.user_id == user.id).limit(1))
    row = result.scalar_one_or_none()
    transactions_model = MODEL_BY_TABLE["points_transactions"]
    tx = await session.execute(select(transactions_model).where(transactions_model.user_id == user.id).order_by(transactions_model.created_at.desc()).limit(100))
    return {"user_id": str(user.id), "points": str(money(row.balance or 0)) if row else "0.00", "transactions": [serialize_record(item) for item in tx.scalars()]}


@router.get("/me/store-credit")
async def me_store_credit(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    refunds_model = MODEL_BY_TABLE["refunds"]
    rows = (
        await session.execute(
            select(refunds_model).where(
                refunds_model.user_id == user.id,
                refunds_model.deleted_at.is_(None),
                refunds_model.status.in_(["completed", "succeeded", "provider_succeeded", "manual_completed"]),
            )
        )
    ).scalars()
    total = Decimal("0.00")
    for row in rows:
        extra = dict(getattr(row, "extra_data", {}) or {})
        method = str(extra.get("refund_method") or extra.get("method") or "").strip().lower()
        if method == "store_credit":
            total += money(getattr(row, "amount", 0))
    return {"store_credit": str(money(total)), "currency_code": "YER"}


@router.get("/local-shopping-requests", operation_id="list_local_shopping_requests")
@router.get("/local_shopping_requests", operation_id="list_local_shopping_requests_legacy")
async def local_shopping_requests(user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["local_shopping_requests"]
    statement = select(model)
    if not roles.intersection({"admin", "manager", "staff"}):
        statement = statement.where(model.user_id == user.id)
    result = await session.execute(statement.order_by(model.created_at.desc()))
    return [serialize_record(row) for row in result.scalars()]


@router.get("/payments/review")
async def payments_review(
    limit: int = Query(500, ge=1, le=1000),
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    require_finance_actor(roles)
    return await list_payment_receipts_for_review(session, limit=limit)


@router.post("/orders/{order_id}/payment-receipt", status_code=201)
async def payment_receipt(order_id: uuid.UUID, request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    return await create_payment_receipt(
        session,
        order_id=order_id,
        request=request,
        user=user,
        roles=roles,
        storage=storage,
    )


@router.post("/payments/{payment_id}/review")
async def review_payment(
    payment_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await review_payment_receipt(
        session,
        payment_id=payment_id,
        body=await request.json(),
        staff=staff,
        roles=roles,
    )


@router.post("/orders/{order_id}/refund", status_code=201)
async def create_refund(
    order_id: uuid.UUID,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    key = str(idempotency_key or body.get("idempotencyKey") or "").strip()
    if not key:
        key = f"order-refund-{uuid.uuid4().hex}"
    digest = request_hash(body)
    endpoint = f"/orders/{order_id}/refund"
    await advisory_xact_lock(session, f"idempotency:{endpoint}:{key}")
    existing = await find_idempotent_refund(
        session,
        actor_id=staff.id,
        endpoint=endpoint,
        key=key,
        request_digest=digest,
    )
    if existing is not None:
        # An idempotent replay must also repair the derived payment state.
        # This matters when the original completion was committed before a
        # receipt review or another financial mutation finished.
        order = await session.get(Order, order_id)
        if order is not None:
            await sync_order_payment_status(session, order)
            await session.commit()
        response.status_code = 200
        payload = financial_response_row(existing)
        if order is not None:
            payload["order_payment_status"] = order.payment_status
        payload["idempotency_replayed"] = True
        return payload
    return await create_refund_request(
        session,
        order_id=order_id,
        body=body,
        staff=staff,
        roles=roles,
        idempotency_key=key,
        request_digest=digest,
        endpoint=endpoint,
    )


@router.post("/receipts/signed-url")
async def receipt_url(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    receipt_ref = str(body.get("receiptId") or body.get("receipt_id") or body.get("receiptPath") or "")
    if not receipt_ref.strip():
        raise HTTPException(status_code=422, detail="receipt_reference_required")
    expires_in_raw = body.get("expires_in")
    if expires_in_raw is None:
        expires_in_raw = body.get("expiresIn")
    return await issue_signed_receipt_url(
        session,
        request=request,
        receipt_ref=receipt_ref,
        user=user,
        roles=roles,
        storage=storage,
        expires_in=expires_in_raw,
    )


@router.get("/receipts/access")
async def receipt_access(token: str, session: AsyncSession = Depends(get_session)):
    return await signed_receipt_file_response(session, token=token, storage=storage)


@router.get("/settings/theme")
async def theme_settings(session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["theme_settings"]
    rows = await _rows(
        session,
        "theme_settings",
        clauses=(
            model.deleted_at.is_(None),
            model.status == "active",
            model.is_active.is_(True),
            ~model.name.like("history:%"),
            ~model.name.like("preview:%"),
        ),
        order_by=(model.updated_at.desc(), model.created_at.desc()),
        limit=1,
    )
    return serialize_record(rows[0]) if rows else {}


@router.put("/settings/theme")
async def save_theme(
    request: Request,
    staff: User = Depends(require_staff),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await ThemeAdminService().save(session, actor=staff, roles=roles, body=await request.json(), setting_key="default", publish=True)


@router.get("/storage/policies")
async def storage_policies(staff: User = Depends(require_staff)):
    return StoragePolicyRegistry.as_dict()


@router.post("/storage/upload", status_code=201)
async def storage_upload(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    return await _secure_upload_from_request(request, user=user, roles=roles, session=session)


@router.post("/storage/presign", status_code=201)
async def storage_presign(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    if not isinstance(body, dict) or UPLOAD_FORBIDDEN_CLIENT_FIELDS.intersection(body.keys()):
        raise HTTPException(status_code=422, detail="invalid_presign_request")
    purpose = str(body.get("purpose") or body.get("category") or "").strip()
    policy = StoragePolicyRegistry.resolve(purpose)
    if policy.visibility != "public":
        raise HTTPException(status_code=422, detail="presigned_upload_public_only")
    presigned = storage.create_presigned_upload(
        policy_key=policy.key,
        file_name=str(body.get("fileName") or body.get("file_name") or "upload.webp"),
        content_type=str(body.get("contentType") or body.get("content_type") or ""),
        size_bytes=body.get("sizeBytes") or body.get("size_bytes") or 0,
        sha256=str(body.get("sha256") or ""),
        roles=roles,
        expires_in=int(body.get("expiresIn") or 900),
    )
    entity_type = str(body.get("entityType") or body.get("entity_type") or "").strip() or None
    entity_id_raw = str(body.get("entityId") or body.get("entity_id") or "").strip()
    entity_id = _uuid(entity_id_raw, "entity_id") if entity_id_raw else None
    asset = FileAsset(
        owner_user_id=user.id,
        created_by=user.id,
        policy_key=policy.key,
        visibility=policy.visibility,
        storage_provider="cloudflare_r2",
        storage_bucket=str(get_settings().r2_bucket),
        storage_key=presigned["storage_key"],
        original_filename=presigned["original_filename"],
        content_type=presigned["content_type"],
        size_bytes=presigned["size_bytes"],
        checksum_sha256=presigned["sha256"],
        status="pending_upload",
        scan_status="not_required",
        scan_provider="presigned-upload-verifier",
        entity_type=entity_type,
        entity_id=entity_id,
        extra_data={
            "upload_mode": "presigned",
            "presign_expires_in": presigned["expires_in"],
            "public_url": presigned["public_url"],
        },
    )
    session.add(asset)
    await session.commit()
    response = _file_asset_response(asset, public_url=presigned["public_url"])
    response.update(
        {
            "upload_url": presigned["upload_url"],
            "headers": presigned["headers"],
            "expires_in": presigned["expires_in"],
            "storage_key": presigned["storage_key"],
            "upload_mode": "presigned",
        }
    )
    return response


@router.post("/storage/complete")
async def storage_complete(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    file_id = str(body.get("fileId") or body.get("file_id") or "").strip() if isinstance(body, dict) else ""
    if not file_id:
        raise HTTPException(status_code=422, detail="file_id_required")
    asset = await session.get(FileAsset, _uuid(file_id, "file_id"))
    if asset is None or asset.deleted_at is not None or not _roles_can_access_file(asset, user, roles):
        raise HTTPException(status_code=404, detail="file_not_found")
    if asset.storage_provider != "cloudflare_r2" or asset.status not in {"pending_upload", "available"}:
        raise HTTPException(status_code=409, detail="file_not_pending_upload")
    if asset.status == "available":
        return _file_asset_response(asset, public_url=_asset_public_url(request, asset))
    try:
        verified = storage.verify_presigned_upload(
            storage_key=asset.storage_key,
            expected_size=asset.size_bytes,
            expected_content_type=asset.content_type,
            expected_sha256=asset.checksum_sha256,
            policy_key=asset.policy_key,
        )
    except HTTPException as exc:
        asset.status = "failed"
        extra_data = dict(asset.extra_data or {})
        extra_data["verification_error"] = str(exc.detail)
        asset.extra_data = extra_data
        await session.commit()
        raise
    asset.status = "available"
    asset.scan_status = verified["scan_status"]
    asset.scan_provider = verified["scan_provider"]
    asset.size_bytes = verified["size_bytes"]
    asset.content_type = verified["content_type"]
    asset.checksum_sha256 = verified["sha256"]
    extra_data = dict(asset.extra_data or {})
    extra_data["verified_at"] = datetime.now(timezone.utc).isoformat()
    asset.extra_data = extra_data
    await session.commit()
    return _file_asset_response(asset, public_url=_asset_public_url(request, asset))


@router.post("/storage/migrate-render-to-r2")
async def storage_migrate_render_to_r2(
    request: Request,
    staff: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json() if request.headers.get("content-type", "").lower().startswith("application/json") else {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="invalid_migration_request")
    apply = bool(body.get("apply", False))
    retention_days = int(body.get("retentionDays") or body.get("retention_days") or 7)
    limit = int(body.get("limit") or 500)
    requested_tables = body.get("tables")
    reference_tables = (
        {str(value).strip() for value in requested_tables if str(value).strip()}
        if isinstance(requested_tables, list)
        else None
    )
    try:
        report = await R2MigrationService(storage).migrate(
            session,
            apply=apply,
            retention_days=retention_days,
            limit=limit,
            actor_id=staff.id,
            reference_tables=reference_tables,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    report["requested_by"] = str(staff.id)
    report["render_files_deleted"] = 0
    return report


@router.post("/storage/remove")
async def storage_remove(request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    if UPLOAD_FORBIDDEN_CLIENT_FIELDS.intersection(body.keys()):
        raise HTTPException(status_code=422, detail="raw_path_delete_forbidden")
    file_id = str(body.get("fileId") or body.get("file_id") or "").strip()
    if not file_id:
        raise HTTPException(status_code=422, detail="file_id_required")
    asset = await session.get(FileAsset, _uuid(file_id, "file_id"))
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="file_not_found")
    if not _roles_can_access_file(asset, user, roles):
        raise HTTPException(status_code=404, detail="file_not_found")
    removed = storage.delete_relative(asset.storage_key, storage_provider=asset.storage_provider)
    asset.deleted_at = datetime.now(timezone.utc)
    asset.deleted_by = user.id
    asset.status = "deleted"
    audit_model = MODEL_BY_TABLE["audit_logs"]
    session.add(
        audit_model(
            user_id=user.id,
            type="file.deleted",
            description=f"Deleted file {asset.id}",
            extra_data={"file_id": str(asset.id), "policy_key": asset.policy_key, "storage_removed": removed},
        )
    )
    await session.commit()
    return {"ok": True, "file_id": str(asset.id), "removed": int(removed)}


@router.post("/manage/product-image", status_code=201)
async def product_image(request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    if not roles.intersection({"admin", "manager", "partner"}):
        raise HTTPException(status_code=403, detail="insufficient_permissions")
    payload = await _secure_upload_from_request(
        request,
        user=user,
        roles=roles,
        session=session,
        forced_policy="product_image",
    )
    payload["imageUrl"] = payload["url"]
    return payload


@router.post("/me/avatar")
async def avatar(request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    payload = await _secure_upload_from_request(
        request,
        user=user,
        roles=roles,
        session=session,
        forced_policy="avatar",
        commit=False,
    )
    result = await session.execute(select(Profile).where(Profile.user_id == user.id).with_for_update())
    profile = result.scalar_one()
    profile.avatar_url = payload["url"]
    await session.commit()
    return {"avatarUrl": payload["url"], "fileId": payload["file_id"], "profile": serialize_record(profile)}


FUNCTION_MAP = {
    "has_role", "is_staff", "approve_partner_application", "can_use_coupon",
    "check_login_rate_limit", "check_password_reset_rate_limit", "close_operational_day",
    "create_admin_notification", "create_order_delay_ticket", "create_user_notification",
    "create_user_notifications", "get_product_likes_count", "increment_coupon_usage",
    "is_identity_banned", "open_operational_day", "redeem_loyalty_points",
    "validate_coupon_for_checkout", "image-search", "ai-product-assistant", "ai-chat-support",
    "ai-enhanced-import", "import-products", "categorize-products", "generate-product-descriptions",
    "product-images", "share-preview", "send-order-email", "whatsapp-notify",
    "notification-fanout", "send-partner-approval", "create-partner-user", "database-backup",
    "security-monitor", "process-email-queue", "delete-account", "request-account-deletion",
    "account-deletion",
}


@router.post("/functions/{function_name}")
async def function_proxy(function_name: str, request: Request, user: User | None = Depends(optional_user), session: AsyncSession = Depends(get_session)):
    if function_name not in FUNCTION_MAP:
        raise HTTPException(status_code=404, detail="function_not_found")
    body = await request.json()
    if function_name == "database-backup":
        if user is None or not set(await roles_for(session, user.id)).intersection({"admin", "manager"}):
            raise HTTPException(status_code=403, detail="admin_required")
        coordinator = BackupCoordinator()
        payload = (
            await coordinator.create_backup(session, actor=user, selected_tables=list((body or {}).get("tables") or []))
            if get_settings().app_env == "test"
            else await coordinator.queue_backup(session, actor=user, selected_tables=list((body or {}).get("tables") or []))
        )
        await session.commit()
        return {"data": payload}
    result = await execute_function(function_name, body, user, session, request)
    await session.commit()
    return result


@router.post("/ai/chat")
async def ai_chat_support_compat(request: Request, user: User | None = Depends(public_optional_user), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    return await execute_public_ai_chat(body, user, session, request)


@router.post("/ai/product-assistant")
async def ai_product_assistant_compat(request: Request, user: User | None = Depends(public_optional_user), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    result = await execute_function("ai-product-assistant", body, user, session, request)
    await session.commit()
    return {"success": True, "result": result}


@router.post("/backups/create")
async def create_backup(request: Request, staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    selected_tables = list((body or {}).get("tables") or [])
    coordinator = BackupCoordinator()
    row = (
        await coordinator.create_backup(session, actor=staff, selected_tables=selected_tables)
        if get_settings().app_env == "test"
        else await coordinator.queue_backup(session, actor=staff, selected_tables=selected_tables)
    )
    _add_audit_log(session, staff.id, "admin.backups.create", "Requested encrypted backup bundle")
    await session.commit()
    return {**row, "data": row}


@router.get("/backups")
async def backups(staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return {"data": await BackupCoordinator().list_backups(session)}


@router.get("/backups/latest")
async def latest_backup(staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    rows = await BackupCoordinator().list_backups(session)
    return {"data": rows[0] if rows else None}


@router.get("/backups/{backup_id}")
async def backup_status(backup_id: uuid.UUID, staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Return one durable backup job status for dashboard polling."""
    model = MODEL_BY_TABLE["backup_records"]
    row = await session.get(model, backup_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="backup_not_found")
    return {"data": BackupCoordinator._response(row)}


@router.get("/backups/{backup_id}/verify")
async def verify_backup(backup_id: uuid.UUID, staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    model = MODEL_BY_TABLE["backup_records"]
    row = await session.get(model, backup_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup_not_found")
    extra = row.extra_data or {}
    return {
        "ok": row.status == "ready" and extra.get("verification_status") == "verified",
        "status": row.status,
        "encrypted": bool(extra.get("encrypted_bundle_key")),
        "offsite_status": extra.get("offsite_status"),
        "restore_verification_status": extra.get("restore_verification_status"),
        "size_bytes": extra.get("size_bytes"),
        "checksum": extra.get("encrypted_checksum"),
    }


@router.get("/backups/{backup_id}/download")
async def download_backup(backup_id: uuid.UUID, staff: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await BackupCoordinator().download(session, backup_id)


@router.post("/api/realtime/tickets", status_code=201)
async def create_realtime_ticket(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    ticket = await RealtimeTicketService().issue(
        session,
        user=user,
        roles=roles,
        requested_channels=[str(item) for item in body.get("channels") or ["notifications"]],
        device_id=str(body.get("deviceId") or body.get("device_id") or ""),
        platform=str(body.get("platform") or "web"),
        origin=request.headers.get("origin"),
        last_event_id=body.get("lastEventId") or body.get("last_event_id"),
    )
    await session.commit()
    return {
        "data": {
            "ticket": ticket.token,
            "expires_at": ticket.expires_at.isoformat(),
            "channels": list(ticket.channels),
            "websocket_url": ticket.websocket_url,
            "protocol": REALTIME_PROTOCOL,
        }
    }


@router.post("/api/realtime/events", status_code=201)
async def create_realtime_event(
    request: Request,
    staff: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    channel = str(body.get("channel") or "").strip()
    if channel not in {"inventory", f"user:{staff.id}", f"courier:{staff.id}"} and not channel.startswith("inventory:partner:"):
        raise HTTPException(status_code=403, detail="realtime_event_channel_denied")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    event = await RealtimeEventService().record_event(
        session,
        channel=channel,
        event=str(body.get("event") or "system.event"),
        payload=payload,
        dedupe_key=body.get("dedupeKey") or body.get("dedupe_key"),
        user_id=staff.id if channel == f"user:{staff.id}" else None,
    )
    await session.commit()
    await realtime_hub.publish_recorded_event(
        channel,
        {
            "type": event.get("event") or body.get("event") or "system.event",
            "event": body.get("event") or "system.event",
            "payload": payload,
            "event_id": event.get("event_id") or event.get("id"),
            "channel": channel,
        },
    )
    return {"data": event}


@router.websocket("/ws/realtime")
async def websocket_secure_updates(websocket: WebSocket):
    if any(key.lower() in {"token", "access_token", "refresh_token"} for key in websocket.query_params.keys()):
        await websocket.close(code=4401)
        return
    raw_ticket = extract_realtime_ticket(websocket)
    if not raw_ticket:
        await websocket.close(code=4401)
        return
    async with SessionFactory() as session:
        try:
            realtime_session = await RealtimeTicketService().consume(
                session,
                ticket=raw_ticket,
                origin=websocket.headers.get("origin"),
            )
            await session.commit()
        except HTTPException as exc:
            await session.rollback()
            await websocket.close(code=4401 if exc.status_code == 401 else 4403)
            return
        except Exception:
            await session.rollback()
            await websocket.close(code=1011)
            return
    try:
        await websocket.accept(subprotocol=REALTIME_PROTOCOL)
        connection = await realtime_hub.register(realtime_session, websocket)
    except HTTPException:
        await websocket.close(code=4429)
        return
    await websocket.send_text(json.dumps({"type": "ready", "channels": sorted(connection.channels)}, ensure_ascii=False))
    async with SessionFactory() as session:
        replay = await RealtimeEventService().replay(session, channels=connection.channels, after_event_id=realtime_session.last_event_id)
    for event in replay:
        await connection.outbound_queue.put(json.dumps(event, ensure_ascii=False, default=str))
    send_task = asyncio.create_task(realtime_hub.send_loop(connection))
    last_pong = datetime.now(timezone.utc)
    settings = get_settings()
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    receive_secure_message(connection),
                    timeout=settings.realtime_heartbeat_interval_seconds,
                )
            except asyncio.TimeoutError:
                if (datetime.now(timezone.utc) - last_pong).total_seconds() > settings.realtime_pong_timeout_seconds:
                    await websocket.close(code=1001)
                    break
                await websocket.send_text(json.dumps({"type": "heartbeat", "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
                continue
            if message is None:
                break
            message_type = str(message.get("type") or "")
            if message_type == "pong":
                last_pong = datetime.now(timezone.utc)
            elif message_type == "subscribe":
                requested = str(message.get("channel") or "")
                if requested in connection.channels:
                    continue
                if requested not in realtime_session.channels or len(connection.channels) >= settings.realtime_max_subscriptions_per_connection:
                    await websocket.close(code=1008)
                    break
                await realtime_hub.subscribe_connection(connection, requested)
            elif message_type == "unsubscribe":
                requested = str(message.get("channel") or "")
                if requested in connection.channels:
                    await realtime_hub.unsubscribe_connection(connection, requested)
            elif message_type == "resume":
                async with SessionFactory() as session:
                    replay = await RealtimeEventService().replay(
                        session,
                        channels=connection.channels,
                        after_event_id=message.get("lastEventId") or message.get("last_event_id"),
                    )
                for event in replay:
                    await connection.outbound_queue.put(json.dumps(event, ensure_ascii=False, default=str))
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        await realtime_hub.disconnect_connection(connection)


@router.websocket("/ws/{channel}")
async def websocket_legacy_rejected(websocket: WebSocket, channel: str):
    await websocket.close(code=4401)
