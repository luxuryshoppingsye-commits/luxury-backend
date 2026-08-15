from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from .api.routes import auth, commerce, e2e_verification, internal, operations, resources
from .config import get_settings
from .database import database_ready, engine
from .models import Base
from .services.api_protection import (
    DistributedRateLimitService,
    REQUEST_ID_HEADER,
    authenticated_subject_from_request,
    policy_for_request,
    request_id_from_request,
    sanitize_request_id,
    set_current_request_id,
)
from .storage import FileStorage


settings = get_settings()
logger = logging.getLogger(__name__)
SEED_UPLOADS_ZIP = Path(__file__).resolve().parents[1] / "seed_data" / "uploads_seed.zip"
PUBLIC_STOREFRONT_ORIGINS = (
    "https://luxuryshoppings.com",
    "https://www.luxuryshoppings.com",
)
CRITICAL_SECURITY_TABLES = (
    "account_security",
    "refresh_token_security",
    "password_reset_token_state",
    "verification_tokens",
    "phone_otp_tokens",
)


SECRET_LOG_PATTERNS = (
    (re.compile(r"([?&](?:token|access_token|refresh_token|ticket)=)[^&\s\"']+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"']+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Sec-WebSocket-Protocol:\s*)([^,\s]+,\s*)?rt\.[^\s\"']+", re.IGNORECASE), r"\1\2rt.***"),
    (re.compile(r"(postgres(?:ql)?(?:\+asyncpg)?://)[^@\s\"']+@", re.IGNORECASE), r"\1***@"),
)


def _redact_log_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    redacted = value
    for pattern, replacement in SECRET_LOG_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_log_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_log_value(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_log_value(value) for key, value in record.args.items()}
        return True


def _install_secret_log_redaction() -> None:
    redaction = SecretRedactionFilter()
    for name in ("uvicorn.access", "uvicorn.error", "fastapi", __name__):
        target_logger = logging.getLogger(name)
        if not any(isinstance(existing, SecretRedactionFilter) for existing in target_logger.filters):
            target_logger.addFilter(redaction)


_install_secret_log_redaction()


def _copy_upload_file(source: Path, target: Path) -> bool:
    if target.is_file() and target.stat().st_size > 0:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _extract_seed_uploads(root: Path) -> int:
    if not SEED_UPLOADS_ZIP.is_file():
        return 0
    extracted = 0
    with zipfile.ZipFile(SEED_UPLOADS_ZIP) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = Path(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if target.is_file() and target.stat().st_size > 0:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            extracted += 1
    return extracted


def _mirror_legacy_product_image_paths(root: Path) -> int:
    source_dir = root / "product-images"
    target_dir = root / "products"
    if not source_dir.is_dir():
        return 0
    copied = 0
    for source in source_dir.rglob("*"):
        if not source.is_file():
            continue
        if _copy_upload_file(source, target_dir / source.relative_to(source_dir)):
            copied += 1
    return copied


def _ensure_runtime_uploads() -> dict[str, int]:
    # Production images are stored directly in Cloudflare R2. Do not seed or
    # mirror image files onto the Render filesystem in that mode.
    if str(getattr(settings, "storage_provider", "local")).strip().lower() == "r2":
        return {"extracted": 0, "mirrored": 0}
    root = settings.resolved_upload_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return {
        "extracted": _extract_seed_uploads(root),
        "mirrored": _mirror_legacy_product_image_paths(root),
    }


async def _ensure_critical_security_tables() -> None:
    """Create non-destructive security tables that older production schemas may lack."""
    async with engine.begin() as connection:
        for table_name in CRITICAL_SECURITY_TABLES:
            table = Base.metadata.tables.get(table_name)
            if table is None:
                logger.warning("Critical security table metadata is missing: %s", table_name)
                continue
            await connection.run_sync(lambda sync_connection, table=table: table.create(sync_connection, checkfirst=True))


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        _ensure_runtime_uploads()
    except (OSError, zipfile.BadZipFile) as exc:
        logger.warning("Runtime upload bootstrap skipped: %s", exc)
    if not await database_ready():
        raise RuntimeError("PostgreSQL is unavailable or DATABASE_URL is invalid")
    try:
        await _ensure_critical_security_tables()
    except SQLAlchemyError as exc:
        logger.error("Critical security table bootstrap failed: %s", exc)
        raise
    message_worker = None
    message_worker_task = None
    worker_flag = os.getenv("ENABLE_MESSAGE_WORKER", "").strip().lower()
    worker_enabled = settings.app_env == "production" or worker_flag in {"1", "true", "yes", "on"}
    if worker_enabled:
        from .workers.message_worker import MessageWorker

        message_worker = MessageWorker(
            worker_id=os.getenv("MESSAGE_WORKER_ID"),
            poll_seconds=settings.message_worker_poll_seconds,
        )
        message_worker_task = asyncio.create_task(message_worker.run_forever())
        logger.info("message worker enabled in web process")
    try:
        yield
    finally:
        if message_worker is not None:
            message_worker.stop()
        if message_worker_task is not None:
            await message_worker_task
        await engine.dispose()


app = FastAPI(
    title="Luxury Shopping API",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

origins = settings.allowed_origins or (
    [
        "http://127.0.0.1",
        "http://localhost",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]
    if settings.app_env in {"development", "test"}
    else []
)
if settings.app_env == "production":
    origins = list(dict.fromkeys([*origins, *PUBLIC_STOREFRONT_ORIGINS]))
local_origin_regex = r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$" if settings.app_env in {"development", "test"} else None
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=local_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Request-ID",
        "Cache-Control",
        "Pragma",
    ],
)
# Compress catalogue/content JSON responses before they cross Render or a CDN.
# Small responses are left untouched to avoid wasting CPU on compression.
app.add_middleware(GZipMiddleware, minimum_size=800, compresslevel=5)


def _read_only_request_allowed(method: str, path: str, body: bytes = b"") -> bool:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if method == "POST" and path.startswith("/resources/") and path.endswith("/query"):
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return False
        return str(payload.get("operation") or "select").lower() == "select"
    return False


PRIVATE_NO_STORE_PREFIXES = (
    "/auth",
    "/api/auth",
    "/me",
    "/profile",
    "/api/profile",
    "/addresses",
    "/cart",
    "/api/cart",
    "/wishlist",
    "/api/wishlist",
    "/favorites",
    "/orders",
    "/api/orders",
    "/payments",
    "/api/payments",
    "/refunds",
    "/api/refunds",
    "/receipts",
    "/notifications",
    "/api/notifications",
    "/support",
    "/api/support",
    "/complaints",
    "/loyalty",
    "/api/loyalty",
    "/coupons",
    "/api/marketing/coupons",
    "/admin",
    "/api/admin",
    "/partner",
    "/api/partner",
    "/merchant",
    "/api/merchant",
    "/operations",
    "/api/operations",
    "/resources",
    "/functions",
    "/storage/upload",
    "/storage/presign",
    "/storage/complete",
    "/storage/migrate-render-to-r2",
)

PUBLIC_CACHEABLE_PREFIXES = (
    "/products",
    "/api/catalog/products",
    "/offers",
    "/api/catalog/offers",
    "/categories",
    "/api/catalog/categories",
    "/brands",
    "/api/catalog/brands",
    "/partner-storefronts",
    "/api/catalog/stores",
    "/settings/theme",
    "/api/settings/theme",
    "/api/content/pages",
    "/api/content/blog",
    "/api/content/menus",
    "/api/content/site",
    "/api/content/social-links",
    "/api/content/theme",
    "/api/content/shipping-zones",
    "/uploads",
    "/api/uploads",
)

# Only JSON catalogue/content responses are eligible for the short in-process
# cache. User/session/order endpoints are deliberately excluded above and are
# always no-store. The cache is cleared after successful writes so admin and
# merchant changes become visible without waiting for the TTL.
PUBLIC_RESPONSE_CACHE_PREFIXES = tuple(
    prefix
    for prefix in PUBLIC_CACHEABLE_PREFIXES
    if prefix not in {"/uploads", "/api/uploads"}
)
try:
    PUBLIC_RESPONSE_CACHE_TTL_SECONDS = max(
        15,
        min(int(os.getenv("PUBLIC_RESPONSE_CACHE_TTL_SECONDS", "300")), 600),
    )
except ValueError:
    PUBLIC_RESPONSE_CACHE_TTL_SECONDS = 300
PUBLIC_RESPONSE_CACHE_MAX_BYTES = 512 * 1024


@dataclass(frozen=True)
class _PublicResponseCacheEntry:
    expires_at: float
    body: bytes
    status_code: int
    media_type: str | None
    headers: tuple[tuple[str, str], ...]


_PUBLIC_RESPONSE_CACHE: dict[str, _PublicResponseCacheEntry] = {}
_PUBLIC_RESPONSE_CACHE_LOCK = asyncio.Lock()


def _public_response_cache_key(request: Request) -> str | None:
    if settings.app_env not in {"production", "staging"}:
        return None
    if request.method != "GET":
        return None
    if request.headers.get("authorization") or request.cookies:
        return None
    path = request.url.path
    if not _matches_prefix(path, PUBLIC_RESPONSE_CACHE_PREFIXES):
        return None
    # Keep Origin and Accept-Encoding in the key because CORS and GZip may emit
    # request-specific response headers/body representations.
    return (
        f"{path}?{request.url.query}"
        f"|origin={request.headers.get('origin', '')}"
        f"|encoding={request.headers.get('accept-encoding', '')}"
    )


async def _get_public_response_cache(key: str) -> _PublicResponseCacheEntry | None:
    now = time.monotonic()
    async with _PUBLIC_RESPONSE_CACHE_LOCK:
        entry = _PUBLIC_RESPONSE_CACHE.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            _PUBLIC_RESPONSE_CACHE.pop(key, None)
            return None
        return entry


async def _clear_public_response_cache() -> None:
    async with _PUBLIC_RESPONSE_CACHE_LOCK:
        _PUBLIC_RESPONSE_CACHE.clear()


async def _materialize_public_response(response: Response) -> tuple[Response, bytes | None]:
    """Materialize a JSON response so it can be safely reused on cache hits."""
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if (
        response.status_code != 200
        or content_type not in {"application/json", "application/problem+json"}
        or response.headers.get("set-cookie")
    ):
        return response, None

    existing_body = getattr(response, "body", None)
    if existing_body is not None:
        body = bytes(existing_body)
    elif hasattr(response, "body_iterator"):
        body = b"".join([chunk async for chunk in response.body_iterator])
    else:
        return response, None
    headers = dict(response.headers)
    # Response recalculates Content-Length from the materialized bytes. Keep
    # CORS headers, but never cache request-specific or compression headers.
    for name in (
        "content-length",
        "date",
        "server",
        "set-cookie",
        "x-request-id",
        "cache-control",
        "pragma",
        "expires",
    ):
        headers.pop(name, None)
    materialized = Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type=getattr(response, "media_type", None),
        background=getattr(response, "background", None),
    )
    return materialized, body


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    normalized = path.lower().rstrip("/") or "/"
    return any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in prefixes)


def _apply_cache_headers(request: Request, response) -> None:
    path = request.url.path
    has_auth_context = bool(request.headers.get("authorization") or request.cookies)
    is_public_get = (
        request.method in {"GET", "HEAD"}
        and not has_auth_context
        and _matches_prefix(path, PUBLIC_CACHEABLE_PREFIXES)
    )
    is_private = (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        or has_auth_context
        or _matches_prefix(path, PRIVATE_NO_STORE_PREFIXES)
    )
    if is_public_get and not is_private:
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        # Cloudflare can use this edge-only TTL while browsers keep the shorter
        # browser max-age. Do not combine s-maxage with stale-while-revalidate:
        # shared caches treat s-maxage as proxy-revalidate.
        response.headers["Cloudflare-CDN-Cache-Control"] = (
            "public, max-age=600, stale-while-revalidate=86400"
        )
        response.headers["Vary"] = "Accept-Encoding, Origin"
        if "Pragma" in response.headers:
            del response.headers["Pragma"]
        if "Expires" in response.headers:
            del response.headers["Expires"]
        return
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Vary"] = "Authorization, Cookie, Origin"


def _apply_security_headers(request: Request, response) -> None:
    path = request.url.path
    response.headers[REQUEST_ID_HEADER] = request_id_from_request(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    response.headers["Cross-Origin-Resource-Policy"] = (
        "cross-origin"
        if request.method in {"GET", "HEAD"} and _matches_prefix(path, ("/uploads", "/api/uploads"))
        else "same-origin"
    )
    if settings.app_env in {"production", "staging"} or request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    _apply_cache_headers(request, response)


ERROR_MESSAGES: dict[str, str] = {
    "password_too_short": "Password does not meet the minimum length.",
    "password_too_long": "Password exceeds the maximum length.",
    "password_policy_violation": "Password does not satisfy the security policy.",
    "password_requires_letters_and_numbers": "Password must include a letter and a number.",
    "current_password_required": "Current password is required.",
    "current_password_invalid": "Current password is invalid.",
    "new_password_same_as_current": "New password must be different from the current password.",
    "invalid_or_expired_reset_token": "Password reset token is invalid or expired.",
    "invalid_reset_token": "Password reset token is invalid or expired.",
    "reset_token_expired": "Password reset token is expired.",
    "invalid_redirect_url": "Password reset redirect URL is not allowed.",
    "email_exists": "Email address is already used.",
    "duplicate_sku": "SKU is already used.",
    "duplicate_variant_sku": "Variant SKU is already used.",
    "invalid_reference": "Referenced record was not found.",
    "database_unavailable": "Database is unavailable.",
    "signed_url_expired": "Signed URL is expired.",
    "signed_url_invalid": "Signed URL is invalid.",
    "invalid_expires_in": "Signed URL expiry value is invalid.",
    "file_access_denied": "File access denied.",
    "permission_denied": "Permission denied.",
    "authentication_required": "Authentication is required.",
    "validation_error": "Request validation failed.",
    "transaction_conflict": "Transaction conflict. Retry the request.",
    "rate_limit_exceeded": "Too many requests. Retry later.",
    "rate_limiter_unavailable": "Request protection is temporarily unavailable.",
    "ai_provider_unconfigured": "AI service is not configured.",
    "ai_provider_failed": "AI service is temporarily unavailable.",
    "ai_permission_denied": "Permission denied.",
    "ai_daily_quota_exceeded": "AI quota exceeded.",
    "ai_monthly_quota_exceeded": "AI quota exceeded.",
    "ai_daily_token_quota_exceeded": "AI quota exceeded.",
    "ai_monthly_token_quota_exceeded": "AI quota exceeded.",
    "ai_daily_cost_quota_exceeded": "AI quota exceeded.",
    "ai_monthly_cost_quota_exceeded": "AI quota exceeded.",
    "ai_concurrency_limit_exceeded": "AI quota exceeded.",
    "ai_model_not_allowed": "Permission denied.",
    "ai_prompt_too_large": "AI request is too large.",
    "ai_provider_selection_denied": "Permission denied.",
    "coupon_usage_checkout_only": "Coupon usage can only be recorded by checkout.",
    "loyalty_redeem_checkout_only": "Loyalty points can only be redeemed by checkout.",
    "request_too_large": "Request body is too large.",
    "invalid_content_length": "Content-Length header is invalid.",
}


ERROR_ALIASES: dict[str, str] = {
    "invalid_current_password": "current_password_invalid",
    "invalid_or_expired_reset_token": "invalid_reset_token",
    "expired_receipt_token": "signed_url_expired",
    "invalid_receipt_token": "signed_url_invalid",
    "insufficient_permissions": "permission_denied",
    "admin_required": "permission_denied",
}


def _request_id(request: Request) -> str:
    return request_id_from_request(request)


def _error_content(request: Request, status_code: int, detail, *, code: str | None = None, message: str | None = None) -> dict:
    request_id = _request_id(request)
    field_errors = {}
    resolved_code = code
    resolved_message = message
    if isinstance(detail, dict):
        resolved_code = resolved_code or str(detail.get("code") or "request_failed")
        resolved_message = resolved_message or str(detail.get("message") or ERROR_MESSAGES.get(resolved_code, resolved_code))
        raw_field_errors = detail.get("field_errors")
        if isinstance(raw_field_errors, dict):
            field_errors = raw_field_errors
    elif isinstance(detail, list):
        resolved_code = resolved_code or "validation_error"
        resolved_message = resolved_message or ERROR_MESSAGES[resolved_code]
        field_errors = {"body": detail}
    else:
        raw_code = str(detail or resolved_code or "request_failed")
        resolved_code = resolved_code or ERROR_ALIASES.get(raw_code, raw_code)
        resolved_message = resolved_message or ERROR_MESSAGES.get(resolved_code, raw_code)
    error = {
        "code": resolved_code,
        "message": resolved_message,
        "field_errors": field_errors,
        "request_id": request_id,
    }
    return {
        "detail": detail,
        "error": error,
        "request_id": request_id,
        "http_status": status_code,
    }


@app.middleware("http")
async def request_security(request: Request, call_next):
    request_id = sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
    request.state.request_id = request_id
    set_current_request_id(request_id)
    policy = policy_for_request(request)

    def guarded_json(status_code: int, detail) -> JSONResponse:
        response = JSONResponse(status_code=status_code, content=_error_content(request, status_code, detail))
        _apply_security_headers(request, response)
        return response

    body = b""
    if settings.read_only_runtime and request.method not in {"GET", "HEAD", "OPTIONS"}:
        body = await request.body()
    if settings.read_only_runtime and not _read_only_request_allowed(
        request.method,
        request.url.path,
        body,
    ):
        return guarded_json(403, "read_only_recovery_qa")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            maximum_request_bytes = policy.maximum_request_bytes or settings.api_max_request_bytes
            if int(content_length) > maximum_request_bytes:
                return guarded_json(413, "request_too_large")
        except ValueError:
            return guarded_json(400, "invalid_content_length")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("content-type", "").lower().startswith("application/json"):
        raw_json_body = await request.body()
        if raw_json_body.strip():
            try:
                json.loads(raw_json_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return guarded_json(400, "invalid_json")
    if policy.authentication_required and authenticated_subject_from_request(request) is None:
        return guarded_json(401, "authentication_required")

    # Public catalogue responses are already bounded by the in-process cache
    # and are also marked cacheable for Cloudflare/browser caches.  Returning a
    # fresh cache hit before the PostgreSQL-backed rate limiter avoids two
    # database round trips for every repeated catalogue request while keeping
    # all uncached requests on the normal limiter path.
    cache_key = _public_response_cache_key(request)
    if cache_key:
        cached = await _get_public_response_cache(cache_key)
        if cached is not None:
            response = Response(
                content=cached.body,
                status_code=cached.status_code,
                headers=dict(cached.headers),
                media_type=cached.media_type,
            )
            _apply_security_headers(request, response)
            logger.info(
                "request_complete request_id=%s method=%s path=%s status=%s policy=%s cache=hit-fast",
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                policy.policy_name,
            )
            return response

    try:
        rate_limit = await DistributedRateLimitService().check(request, policy)
    except HTTPException as exc:
        response = JSONResponse(status_code=exc.status_code, content=_error_content(request, exc.status_code, exc.detail))
        _apply_security_headers(request, response)
        return response
    if not rate_limit.allowed:
        response = JSONResponse(
            status_code=429,
            content=_error_content(
                request,
                429,
                {"code": "rate_limit_exceeded", "message": ERROR_MESSAGES["rate_limit_exceeded"]},
            ),
        )
        for key, value in rate_limit.headers.items():
            response.headers[key] = value
        _apply_security_headers(request, response)
        return response

    if cache_key:
        cached = await _get_public_response_cache(cache_key)
        if cached is not None:
            response = Response(
                content=cached.body,
                status_code=cached.status_code,
                headers=dict(cached.headers),
                media_type=cached.media_type,
            )
            for key, value in rate_limit.headers.items():
                response.headers.setdefault(key, value)
            _apply_security_headers(request, response)
            logger.info(
                "request_complete request_id=%s method=%s path=%s status=%s policy=%s cache=hit",
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                policy.policy_name,
            )
            return response

    response = await call_next(request)
    for key, value in rate_limit.headers.items():
        response.headers.setdefault(key, value)
    _apply_security_headers(request, response)

    if cache_key:
        response, body = await _materialize_public_response(response)
        _apply_security_headers(request, response)
        if body is not None and len(body) <= PUBLIC_RESPONSE_CACHE_MAX_BYTES:
            cached_headers = tuple(
                (key, value)
                for key, value in response.headers.items()
                if key.lower()
                not in {
                    "content-length",
                    "date",
                    "server",
                    "set-cookie",
                    "x-request-id",
                    "cache-control",
                    "pragma",
                    "expires",
                }
                and not key.lower().startswith("x-ratelimit-")
            )
            async with _PUBLIC_RESPONSE_CACHE_LOCK:
                _PUBLIC_RESPONSE_CACHE[cache_key] = _PublicResponseCacheEntry(
                    expires_at=time.monotonic() + PUBLIC_RESPONSE_CACHE_TTL_SECONDS,
                    body=body,
                    status_code=response.status_code,
                    media_type=getattr(response, "media_type", None),
                    headers=cached_headers,
                )
    elif request.method not in {"GET", "HEAD", "OPTIONS"} and response.status_code < 400:
        # Any successful write can change a public product/content response.
        await _clear_public_response_cache()

    logger.info(
        "request_complete request_id=%s method=%s path=%s status=%s policy=%s cache=%s",
        request_id,
        request.method,
        request.url.path,
        getattr(response, "status_code", 0),
        policy.policy_name,
        "stored" if cache_key else "bypass",
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    response = JSONResponse(status_code=exc.status_code, content=_error_content(request, exc.status_code, exc.detail))
    _apply_security_headers(request, response)
    return response


@app.exception_handler(json.JSONDecodeError)
async def json_decode_exception_handler(request: Request, exc: json.JSONDecodeError):
    response = JSONResponse(status_code=400, content=_error_content(request, 400, "invalid_json"))
    _apply_security_headers(request, response)
    return response


@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
    response = JSONResponse(status_code=422, content=_error_content(request, 422, exc.errors(), code="validation_error"))
    _apply_security_headers(request, response)
    return response


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    if any(error.get("type") == "json_invalid" for error in errors):
        response = JSONResponse(status_code=400, content=_error_content(request, 400, "invalid_json"))
        _apply_security_headers(request, response)
        return response
    response = JSONResponse(status_code=422, content=_error_content(request, 422, errors, code="validation_error"))
    _apply_security_headers(request, response)
    return response


def _integrity_error_detail(exc: IntegrityError) -> tuple[int, dict[str, str]]:
    orig = getattr(exc, "orig", None)
    sqlstate = str(getattr(orig, "sqlstate", "") or getattr(orig, "pgcode", "") or "")
    constraint = str(getattr(orig, "constraint_name", "") or "")
    lowered = f"{constraint} {orig} {exc}".lower()
    if sqlstate == "23505":
        if "categories" in lowered and "slug" in lowered:
            return 409, {"code": "duplicate_category_slug", "message": "Category slug is already used"}
        if "categories" in lowered and "name" in lowered:
            return 409, {"code": "duplicate_category_name", "message": "Category name is already used"}
        if "sku" in lowered and "variant" in lowered:
            return 409, {"code": "duplicate_variant_sku", "message": "Variant SKU is already used"}
        if "sku" in lowered:
            return 409, {"code": "duplicate_sku", "message": "SKU is already used"}
        if "slug" in lowered:
            return 409, {"code": "duplicate_product_slug", "message": "Product slug is already used"}
        return 409, {"code": "duplicate_record", "message": "Record already exists"}
    if sqlstate == "23503":
        return 422, {"code": "invalid_reference", "message": "Referenced record was not found"}
    if sqlstate == "23502":
        return 422, {"code": "missing_required_field", "message": "A required field is missing"}
    if sqlstate == "23514":
        if "price" in lowered:
            return 422, {"code": "invalid_price", "message": "Product price is invalid"}
        if "stock" in lowered:
            return 422, {"code": "invalid_stock", "message": "Product stock is invalid"}
        return 422, {"code": "constraint_violation", "message": "Submitted data violates a database rule"}
    return 409, {"code": "integrity_conflict", "message": "Submitted data conflicts with existing records"}


@app.exception_handler(IntegrityError)
async def integrity_exception_handler(request: Request, exc: IntegrityError):
    status_code, detail = _integrity_error_detail(exc)
    response = JSONResponse(status_code=status_code, content=_error_content(request, status_code, detail))
    _apply_security_headers(request, response)
    return response


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    orig = getattr(exc, "orig", None)
    sqlstate = str(getattr(orig, "sqlstate", "") or getattr(orig, "pgcode", "") or "")
    if sqlstate in {"40001", "40P01"}:
        response = JSONResponse(status_code=409, content=_error_content(request, 409, {"code": "transaction_conflict", "message": "Please retry the request"}))
        _apply_security_headers(request, response)
        return response
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        response = JSONResponse(status_code=503, content=_error_content(request, 503, {"code": "database_unavailable", "message": "Database is unavailable"}))
        _apply_security_headers(request, response)
        return response
    response = JSONResponse(status_code=503, content=_error_content(request, 503, "database_unavailable"))
    _apply_security_headers(request, response)
    return response


app.include_router(auth.router)
app.include_router(commerce.router)
app.include_router(operations.router)
app.include_router(resources.router)
app.include_router(e2e_verification.router)
app.include_router(internal.router)
app.include_router(auth.router, prefix="/api", include_in_schema=False)
app.include_router(commerce.router, prefix="/api", include_in_schema=False)
app.include_router(operations.router, prefix="/api", include_in_schema=False)
app.include_router(operations.router, prefix="/api/operations", include_in_schema=False)
app.include_router(resources.router, prefix="/api", include_in_schema=False)


def _upload_media_type(path) -> str:
    data = path.read_bytes()[:16]
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


@app.get("/uploads/{file_path:path}")
async def local_upload(file_path: str, request: Request, format: str | None = None):
    return await _serve_upload(file_path, request=request, requested_format=format)


@app.head("/uploads/{file_path:path}")
async def local_upload_head(file_path: str, request: Request, format: str | None = None):
    return await _serve_upload(file_path, request=request, requested_format=format)


@app.get("/api/uploads/{file_path:path}")
async def api_upload(file_path: str, request: Request, format: str | None = None):
    return await _serve_upload(file_path, request=request, requested_format=format)


@app.head("/api/uploads/{file_path:path}")
async def api_upload_head(file_path: str, request: Request, format: str | None = None):
    return await _serve_upload(file_path, request=request, requested_format=format)


async def _serve_upload(file_path: str, *, request: Request, requested_format: str | None = None):
    if not FileStorage.is_public_relative_path(file_path):
        raise HTTPException(status_code=404, detail="file_not_found")
    root = settings.resolved_upload_dir.resolve()
    target = (root / file_path).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=404, detail="file_not_found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file_not_found")
    transcoded = _transcoded_public_upload_response(target, requested_format)
    if transcoded is not None:
        return transcoded
    return FileResponse(
        target,
        media_type=_upload_media_type(target),
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


def _transcoded_public_upload_response(target: Path, requested_format: str | None) -> Response | None:
    output_format = (requested_format or "").strip().lower()
    if output_format not in {"png", "jpeg", "jpg"}:
        return None
    if target.suffix.lower() != ".webp":
        return None
    try:
        from PIL import Image
    except Exception:
        logger.warning("Pillow is unavailable for public upload transcoding")
        return None
    pil_format = "JPEG" if output_format in {"jpeg", "jpg"} else "PNG"
    media_type = "image/jpeg" if pil_format == "JPEG" else "image/png"
    with Image.open(target) as image:
        if pil_format == "JPEG":
            if image.mode in {"RGBA", "LA", "P"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
        elif image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        output = BytesIO()
        image.save(output, format=pil_format, optimize=True)
    return Response(
        output.getvalue(),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )
