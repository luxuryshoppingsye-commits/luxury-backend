from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
from collections import deque
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import engine
from ..security.tokens import decode_token


REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
_request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    limit: int
    window_seconds: int
    key_parts: tuple[str, ...] = ("user_or_ip", "policy", "path")
    fail_closed: bool = True


@dataclass(frozen=True)
class ApiProtectionPolicy:
    policy_name: str
    authentication_required: bool
    required_permissions: tuple[str, ...] = ()
    allowed_roles: tuple[str, ...] = ()
    rate_limit_policy: str = "public_read"
    rate_limit_keys: tuple[str, ...] = ("user_or_ip", "policy", "path")
    maximum_request_bytes: int | None = None
    maximum_response_rows: int = 100
    maximum_page_size: int = 100
    maximum_filter_count: int = 8
    timeout_seconds: int = 15
    idempotency_required: bool = False
    audit_required: bool = False
    cache_policy: str = "no-store"
    security_headers_policy: str = "standard"
    sensitive_response: bool = False
    request_id_required: bool = True
    public_or_private: str = "private"
    ai_quota_policy: str | None = None


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after: int | None = None
    storage: str = "postgresql"
    policy_name: str = ""

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(self.remaining, 0)),
            "RateLimit-Reset": str(max(self.reset_seconds, 0)),
        }
        if self.retry_after is not None:
            headers["Retry-After"] = str(max(self.retry_after, 1))
        return headers


# Public catalog and health reads already have fail-open protection policies.
# Keeping their short sliding windows in the process avoids a PostgreSQL
# transaction on every cache miss, while authenticated, write, finance, and
# internal routes continue to use the distributed PostgreSQL limiter below.
_LOCAL_PUBLIC_RATE_LIMIT_LOCK = asyncio.Lock()
_LOCAL_PUBLIC_RATE_LIMIT_EVENTS: dict[str, deque[float]] = {}
_LOCAL_PUBLIC_RATE_LIMIT_MAX_KEYS = 4096


async def _check_local_public_rate_limit(
    *,
    key: str,
    limit: int,
    window_seconds: int,
    policy_name: str,
) -> RateLimitDecision:
    now = time.monotonic()
    cutoff = now - window_seconds
    async with _LOCAL_PUBLIC_RATE_LIMIT_LOCK:
        events = _LOCAL_PUBLIC_RATE_LIMIT_EVENTS.setdefault(key, deque())
        while events and events[0] <= cutoff:
            events.popleft()

        if len(events) >= limit:
            reset_seconds = max(1, int(events[0] + window_seconds - now) + 1)
            return RateLimitDecision(
                False,
                limit,
                0,
                reset_seconds,
                retry_after=reset_seconds,
                storage="process_memory",
                policy_name=policy_name,
            )

        events.append(now)
        if len(_LOCAL_PUBLIC_RATE_LIMIT_EVENTS) > _LOCAL_PUBLIC_RATE_LIMIT_MAX_KEYS:
            for candidate, candidate_events in list(_LOCAL_PUBLIC_RATE_LIMIT_EVENTS.items()):
                if not candidate_events:
                    _LOCAL_PUBLIC_RATE_LIMIT_EVENTS.pop(candidate, None)
            while len(_LOCAL_PUBLIC_RATE_LIMIT_EVENTS) > _LOCAL_PUBLIC_RATE_LIMIT_MAX_KEYS:
                _LOCAL_PUBLIC_RATE_LIMIT_EVENTS.pop(next(iter(_LOCAL_PUBLIC_RATE_LIMIT_EVENTS)), None)

        return RateLimitDecision(
            True,
            limit,
            max(limit - len(events), 0),
            window_seconds,
            storage="process_memory",
            policy_name=policy_name,
        )


PUBLIC_GET_PREFIXES = (
    "/health",
    "/api/health",
    "/health/live",
    "/health/ready",
    "/version",
    "/api/version",
    "/deployment/status",
    "/products",
    "/api/catalog/products",
    "/api/catalog/recommendations",
    "/offers",
    "/api/catalog/offers",
    "/categories",
    "/api/catalog/categories",
    "/brands",
    "/api/catalog/brands",
    "/stores",
    "/partners",
    "/partner-storefronts",
    "/api/catalog/stores",
    "/api/catalog/currencies",
    "/api/loyalty/tiers",
    "/settings/theme",
    "/api/catalog/settings",
    "/api/catalog/banners",
    "/api/payments/accounts",
    "/api/payment-methods",
    "/api/shopping/global-sites",
    "/api/shopping/local/options",
    "/api/shopping/local/partners",
    "/api/content/pages",
    "/api/content/blog",
    "/api/content/custom-elements",
    "/api/content/menus",
    "/api/content/site",
    "/api/content/social-links",
    "/api/content/theme",
    "/api/content/settings/public",
    "/api/content/theme/preview",
    # Compatibility routes used by older storefront clients.  These are
    # read-only public content endpoints too; leaving them out makes the
    # security middleware classify them as private and return 401 before the
    # cached route handler can run.
    "/content/site",
    "/content/menus",
    "/content/social-links",
    "/content/theme",
    "/content/settings/public",
    "/content/sections",
    "/content/pages",
    "/api/content/shipping-zones",
    "/api/content/",
    "/api/marketing/campaigns/active",
    "/api/suppliers/counts/products",
    "/api/engagement/products",
    "/api/reviews/store/public",
    "/uploads",
    "/api/uploads",
    "/share/products",
)
PUBLIC_POST_PATHS = frozenset(
    {
        "/ai/chat",
        "/ai/product-assistant",
        "/api/ai/chat",
        "/api/ai/product-assistant",
        "/api/operations/ai/chat",
        "/api/operations/ai/product-assistant",
        "/api/catalog/cart/hydrate",
    }
)
SEARCH_PREFIXES = (
    "/products",
    "/api/catalog/products",
    "/categories",
    "/api/catalog/categories",
    "/brands",
    "/api/catalog/brands",
)
UPLOAD_PREFIXES = (
    "/storage/upload",
    "/storage/presign",
    "/storage/complete",
    "/storage/migrate-render-to-r2",
    "/manage/product-image",
    "/me/avatar",
)
SUPPORT_PREFIXES = (
    "/support",
    "/api/support",
    "/support-tickets",
    "/support/tickets",
    "/complaints",
)
FINANCE_PREFIXES = (
    "/payments",
    "/api/payments",
    "/refunds",
    "/api/refunds",
    "/receipts",
    "/finance",
    "/api/finance",
    "/reports",
    "/admin/reports",
)
ADMIN_PREFIXES = (
    "/admin",
    "/api/admin",
    "/api/admin-data",
    "/api/admin-shopping",
    "/api/dashboard",
    "/api/operations",
    "/api/catalog/admin",
    "/api/partnership",
    "/api/loyalty",
    "/api/marketing",
    "/marketing",
    "/api/analytics",
    "/api/reviews/store/admin",
    "/api/notifications/admin",
    "/admin-notifications",
    "/api/content/settings/admin",
    "/api/content/pages",
    "/api/content/theme/history",
    "/api/content/theme/templates",
    "/notifications/outbox",
    "/email/outbox",
    "/whatsapp/outbox",
    "/api/internal/messaging",
    "/api/suppliers",
    "/backups",
)
MERCHANT_PREFIXES = (
    "/partner",
    "/api/partner",
    "/merchant",
    "/api/merchant",
    "/manage/products",
    "/manage/brands",
    "/manage/suppliers",
    "/marketer",
)
AUTH_PREFIXES = (
    "/auth",
    "/api/auth",
)
AI_FUNCTIONS = frozenset(
    {
        "image-search",
        "product-images",
        "ai-product-assistant",
        "ai-chat-support",
        "ai-enhanced-import",
        "categorize-products",
        "generate-product-descriptions",
    }
)
AI_GENERATION_FUNCTIONS = frozenset(
    {"ai-enhanced-import", "categorize-products", "generate-product-descriptions"}
)
AI_RECOMMENDATION_FUNCTIONS = frozenset({"image-search", "product-images", "ai-product-assistant", "ai-chat-support"})
PUBLIC_FUNCTIONS = frozenset({"get_product_likes_count", "share-preview"})


def sanitize_request_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def set_current_request_id(value: str) -> None:
    _request_id_context.set(value)


def current_request_id() -> str:
    return _request_id_context.get() or uuid.uuid4().hex


def request_id_from_request(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    return sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))


def _normalize_path(path: str) -> str:
    cleaned = (path or "/").split("?", 1)[0].strip().lower()
    if not cleaned:
        return "/"
    return cleaned if cleaned.startswith("/") else f"/{cleaned}"


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    normalized = _normalize_path(path)
    return any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in prefixes)


def _function_name_from_path(path: str) -> str | None:
    normalized = _normalize_path(path)
    prefix = "/functions/"
    if not normalized.startswith(prefix):
        return None
    name = normalized[len(prefix) :].split("/", 1)[0]
    return name or None


def _is_public_product_review_read(path: str) -> bool:
    parts = _normalize_path(path).strip("/").split("/")
    if len(parts) == 4 and parts[:3] == ["api", "reviews", "products"]:
        return True
    return len(parts) == 5 and parts[:3] == ["api", "reviews", "products"] and parts[4] == "stats"


def _settings_rate_policies() -> dict[str, RateLimitPolicy]:
    settings = get_settings()
    return {
        "public_read": RateLimitPolicy("public_read", settings.public_read_rate_limit, 60, fail_closed=False),
        "authentication": RateLimitPolicy("authentication", settings.authentication_rate_limit, 900),
        "search": RateLimitPolicy("search", settings.search_rate_limit_auth, 60, fail_closed=False),
        "search_anon": RateLimitPolicy("search_anon", settings.search_rate_limit_anon, 60, fail_closed=False),
        "upload": RateLimitPolicy("upload", settings.upload_rate_limit, 60),
        "support_write": RateLimitPolicy("support_write", settings.support_rate_limit, 86400),
        "generic_resource": RateLimitPolicy("generic_resource", settings.resource_rate_limit, 60),
        "customer_write": RateLimitPolicy("customer_write", settings.customer_write_rate_limit, 60),
        "merchant_write": RateLimitPolicy("merchant_write", settings.merchant_write_rate_limit, 60),
        "admin_write": RateLimitPolicy("admin_write", settings.admin_write_rate_limit, 60),
        "finance_write": RateLimitPolicy("finance_write", settings.finance_write_rate_limit, 60),
        "ai_generation": RateLimitPolicy("ai_generation", settings.ai_rate_limit, 60),
        "ai_recommendation": RateLimitPolicy("ai_recommendation", settings.ai_rate_limit, 60),
        "internal_worker": RateLimitPolicy("internal_worker", settings.internal_worker_rate_limit, 60),
        "internal_diagnostics": RateLimitPolicy("internal_diagnostics", settings.internal_diagnostics_rate_limit, 60),
    }


def _policy(
    name: str,
    *,
    auth: bool,
    roles: tuple[str, ...] = (),
    permissions: tuple[str, ...] = (),
    rate: str | None = None,
    public: bool = False,
    sensitive: bool = False,
    audit: bool = False,
    idempotency: bool = False,
    ai_quota: str | None = None,
) -> ApiProtectionPolicy:
    settings = get_settings()
    maximum_request_bytes = settings.max_upload_bytes if name == "upload" else settings.api_max_request_bytes
    return ApiProtectionPolicy(
        policy_name=name,
        authentication_required=auth,
        required_permissions=permissions,
        allowed_roles=roles,
        rate_limit_policy=rate or name,
        maximum_request_bytes=maximum_request_bytes,
        maximum_response_rows=settings.resource_admin_max_page_size if name in {"admin_write", "finance_write"} else settings.resource_max_page_size,
        maximum_page_size=settings.resource_admin_max_page_size if name in {"admin_write", "finance_write"} else settings.resource_max_page_size,
        maximum_filter_count=settings.resource_max_filters,
        timeout_seconds=settings.api_route_timeout_seconds,
        idempotency_required=idempotency,
        audit_required=audit,
        cache_policy="public" if public else "no-store",
        sensitive_response=sensitive,
        public_or_private="public" if public else "private",
        ai_quota_policy=ai_quota,
    )


def policy_for_request(request: Request) -> ApiProtectionPolicy:
    return policy_for_route(request.method, request.url.path)


def policy_for_route(method: str, path: str) -> ApiProtectionPolicy:
    method = method.upper()
    normalized = _normalize_path(path)
    function_name = _function_name_from_path(normalized)
    if method == "OPTIONS":
        return _policy("public_read", auth=False, rate="public_read", public=True)
    if normalized.startswith("/docs") or normalized.startswith("/redoc") or normalized.startswith("/openapi.json"):
        return _policy("internal_diagnostics", auth=False, rate="internal_diagnostics", public=True)
    if normalized.startswith("/internal"):
        return _policy("internal_diagnostics", auth=True, roles=("admin", "manager"), rate="internal_diagnostics", sensitive=True)
    if function_name in PUBLIC_FUNCTIONS:
        return _policy("public_read", auth=False, rate="public_read", public=True)
    if function_name in AI_GENERATION_FUNCTIONS:
        return _policy("ai_generation", auth=True, permissions=("ai:generate_content",), rate="ai_generation", sensitive=True, audit=True, ai_quota="generation")
    if function_name in AI_RECOMMENDATION_FUNCTIONS:
        return _policy("ai_recommendation", auth=True, permissions=("ai:use",), rate="ai_recommendation", sensitive=True, audit=True, ai_quota="recommendation")
    if function_name:
        return _policy("customer_write", auth=True, rate="customer_write", sensitive=True, audit=True)
    if _matches(normalized, AUTH_PREFIXES):
        return _policy("authentication", auth=False, rate="authentication", sensitive=True)
    if normalized.startswith("/e2e/"):
        return _policy("e2e_verification", auth=False, rate="internal_diagnostics", sensitive=True, audit=method != "GET")
    if method == "POST" and normalized == "/api/analytics/events":
        return _policy("public_write", auth=False, rate="public_read", public=True, audit=False)
    if method == "POST" and normalized in PUBLIC_POST_PATHS:
        return _policy("public_read", auth=False, rate="public_read", public=True)
    if method in {"GET", "HEAD"} and (_matches(normalized, PUBLIC_GET_PREFIXES) or _is_public_product_review_read(normalized)):
        return _policy("public_read", auth=False, rate="public_read", public=True)
    if _matches(normalized, UPLOAD_PREFIXES) or normalized.endswith("/payment-receipt"):
        return _policy("upload", auth=True, rate="upload", sensitive=True, audit=True)
    if _matches(normalized, SUPPORT_PREFIXES):
        return _policy("support_write" if method != "GET" else "customer_write", auth=True, rate="support_write" if method != "GET" else "customer_write", sensitive=True, audit=method != "GET")
    if normalized.startswith("/resources/"):
        return _policy("generic_resource", auth=False, permissions=("resources:generic_write",), rate="generic_resource", sensitive=True, audit=method != "GET")
    # A signed receipt URL is intentionally bearer-token-only. Requiring a
    # second Authorization header defeats the purpose of the URL returned to
    # the customer/admin UI and causes valid receipt previews to return 401.
    # Keep it uncached and let signed_receipt_file_response validate the token.
    if method == "GET" and normalized == "/receipts/access":
        return _policy("public_read", auth=False, rate="public_read", sensitive=True)
    if method == "GET" and normalized == "/api/marketing/campaigns/active":
        return _policy("public_read", auth=False, rate="public_read", public=True)
    if _matches(normalized, FINANCE_PREFIXES):
        return _policy("finance_write", auth=True, roles=("admin", "manager", "finance"), rate="finance_write", sensitive=True, audit=method != "GET")
    if _matches(normalized, ADMIN_PREFIXES):
        return _policy("admin_write", auth=True, roles=("admin", "manager", "staff", "employee", "finance", "logistics"), rate="admin_write", sensitive=True, audit=method != "GET")
    if _matches(normalized, MERCHANT_PREFIXES):
        # The admin product UI uses the compatibility /manage/products paths
        # for staff members too. Route-level ownership checks still protect
        # partner records, but the policy must not reject admin staff first.
        return _policy("merchant_write", auth=True, roles=("partner", "admin", "manager", "staff", "employee", "logistics"), rate="merchant_write", sensitive=True, audit=method != "GET")
    if _matches(normalized, SEARCH_PREFIXES):
        return _policy("search", auth=False, rate="search", public=True)
    if normalized.startswith("/delivery/"):
        return _policy("customer_write", auth=True, rate="customer_write", sensitive=True, audit=method != "GET")
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return _policy("customer_write", auth=True, rate="customer_write", sensitive=True, audit=True, idempotency=method == "POST")
    return _policy("customer_write", auth=True, rate="customer_write", sensitive=True, audit=False)


def route_policy_coverage(app: Any) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    unclassified: list[dict[str, str]] = []
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", "")
        methods = sorted((getattr(route, "methods", None) or {"GET"}) - {"HEAD", "OPTIONS"})
        if not path or path.startswith("/openapi"):
            continue
        for method in methods:
            policy = policy_for_route(method, path)
            row = {
                "method": method,
                "path": path,
                "policy_name": policy.policy_name,
                "authentication_required": policy.authentication_required,
                "required_permissions": list(policy.required_permissions),
                "allowed_roles": list(policy.allowed_roles),
                "rate_limit_policy": policy.rate_limit_policy,
            }
            rows.append(row)
            if not policy.policy_name:
                unclassified.append({"method": method, "path": path})
    return {
        "total_routes": len(rows),
        "classified_routes": len(rows) - len(unclassified),
        "unclassified_routes": len(unclassified),
        "routes": rows,
    }


def _secret_key() -> bytes:
    settings = get_settings()
    return settings.jwt_secret.encode("utf-8")


def hmac_digest(value: str) -> str:
    return hmac.new(_secret_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _trusted_networks() -> list[ipaddress._BaseNetwork]:
    settings = get_settings()
    configured = settings.trusted_proxy_cidrs or settings.trusted_proxy_ips
    networks: list[ipaddress._BaseNetwork] = []
    for raw in configured.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            if "/" not in item:
                address = ipaddress.ip_address(item)
                item = f"{item}/32" if address.version == 4 else f"{item}/128"
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return networks


def _ip_in_networks(value: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _parse_forwarded_for(value: str) -> list[str]:
    result: list[str] = []
    for item in value.split(","):
        candidate = item.strip().strip('"')
        if not candidate:
            continue
        if candidate.lower().startswith("for="):
            candidate = candidate[4:].strip().strip('"')
        if candidate.startswith("[") and "]" in candidate:
            candidate = candidate[1 : candidate.index("]")]
        elif ":" in candidate and candidate.count(":") == 1:
            candidate = candidate.split(":", 1)[0]
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        result.append(candidate)
    return result


def trusted_client_ip(request: Request | None) -> str:
    if request is None or request.client is None:
        return "unknown"
    peer = request.client.host or "unknown"
    networks = _trusted_networks()
    if not _ip_in_networks(peer, networks):
        return peer
    forwarded_values: list[str] = []
    forwarded_values.extend(_parse_forwarded_for(request.headers.get("forwarded", "")))
    forwarded_values.extend(_parse_forwarded_for(request.headers.get("x-forwarded-for", "")))
    for candidate in forwarded_values:
        if not _ip_in_networks(candidate, networks):
            return candidate
    return forwarded_values[0] if forwarded_values else peer


def _subject_from_request(request: Request) -> uuid.UUID | None:
    auth = request.headers.get("authorization") or ""
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.cookies.get("at") or ""
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        return uuid.UUID(str(payload.get("sub")))
    except (jwt.PyJWTError, ValueError, TypeError):
        return None


def authenticated_subject_from_request(request: Request) -> uuid.UUID | None:
    return _subject_from_request(request)


def _rate_identity(request: Request, policy: ApiProtectionPolicy) -> tuple[str, uuid.UUID | None]:
    user_id = _subject_from_request(request)
    ip_hash = hmac_digest(f"ip:{trusted_client_ip(request)}")[:32]
    if user_id is not None:
        return f"user:{hmac_digest(str(user_id))[:32]}:ip:{ip_hash}", user_id
    return f"anon:{ip_hash}", None


def _advisory_lock_id(key: str) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class DistributedRateLimitService:
    storage = "postgresql"

    async def check(self, request: Request, policy: ApiProtectionPolicy) -> RateLimitDecision:
        settings = get_settings()
        rate_policy = _settings_rate_policies()[policy.rate_limit_policy]
        if rate_policy.name == "search" and _subject_from_request(request) is None:
            rate_policy = _settings_rate_policies()["search_anon"]
        if settings.read_only_runtime:
            return RateLimitDecision(True, rate_policy.limit, rate_policy.limit, rate_policy.window_seconds, policy_name=rate_policy.name)
        if rate_policy.limit <= 0:
            return RateLimitDecision(True, 0, 0, rate_policy.window_seconds, policy_name=rate_policy.name)
        identity, user_id = _rate_identity(request, policy)
        normalized_path = _normalize_path(request.url.path)
        key_material = f"{settings.app_env}:{rate_policy.name}:{request.method.upper()}:{normalized_path}:{identity}"
        key_hash = hmac_digest(key_material)
        if policy.public_or_private == "public" and not rate_policy.fail_closed:
            return await _check_local_public_rate_limit(
                key=key_hash,
                limit=rate_policy.limit,
                window_seconds=rate_policy.window_seconds,
                policy_name=rate_policy.name,
            )

        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=rate_policy.window_seconds)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("select pg_advisory_xact_lock(:lock_id)"), {"lock_id": _advisory_lock_id(key_hash)})
                current = int(
                    (
                        await connection.execute(
                            text(
                                """
                                select count(*)
                                from security_events
                                where type = 'api_rate_limit'
                                  and description = :key_hash
                                  and created_at >= :since
                                """
                            ),
                            {"key_hash": key_hash, "since": since},
                        )
                    ).scalar_one()
                    or 0
                )
                reset_seconds = rate_policy.window_seconds
                if current >= rate_policy.limit:
                    return RateLimitDecision(
                        False,
                        rate_policy.limit,
                        0,
                        reset_seconds,
                        retry_after=reset_seconds,
                        policy_name=rate_policy.name,
                    )
                await connection.execute(
                    text(
                        """
                        insert into security_events (id, user_id, type, status, description, path, extra_data)
                        values (:id, :user_id, 'api_rate_limit', 'allowed', :key_hash, :path, cast(:extra_data as jsonb))
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "user_id": user_id,
                        "key_hash": key_hash,
                        "path": normalized_path,
                        "extra_data": json.dumps(
                            {
                                "policy": rate_policy.name,
                                "request_id": request_id_from_request(request),
                                "identity_hash": identity,
                                "storage": self.storage,
                                "created_epoch": int(time.time()),
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
                return RateLimitDecision(
                    True,
                    rate_policy.limit,
                    rate_policy.limit - current - 1,
                    reset_seconds,
                    policy_name=rate_policy.name,
                )
        except Exception:
            if rate_policy.fail_closed:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "rate_limiter_unavailable", "message": "Request protection is temporarily unavailable."},
                )
            return RateLimitDecision(True, rate_policy.limit, max(rate_policy.limit - 1, 0), rate_policy.window_seconds, policy_name=rate_policy.name)


def capabilities_for_roles(roles: set[str], permissions: set[str] | None = None) -> dict[str, Any]:
    is_admin = bool(roles.intersection({"admin", "manager"}))
    is_staff = bool(roles.intersection({"admin", "manager", "finance", "logistics", "staff", "employee"}))
    is_partner = "partner" in roles
    capabilities = {
        "ai:use": bool(roles),
        "ai:use_assistant": bool(roles),
        "ai:use_recommendations": bool(roles),
        "ai:generate_content": is_admin or is_partner,
        "ai:admin": is_admin,
        "ai:view_usage": is_admin,
        "admin:write": is_staff,
        "finance:write": bool(roles.intersection({"admin", "manager", "finance"})),
        "merchant:write": is_partner or is_admin,
        "support:write": bool(roles),
        "upload:write": bool(roles),
        "resources:generic_write": bool(roles),
    }
    if permissions is not None:
        # The dashboard-specific permissions are additive to the legacy
        # capability names above.  This keeps existing integrations stable
        # while allowing per-employee checkbox control in the Windows UI.
        from .staff_permissions import capabilities_for_permissions

        capabilities.update(capabilities_for_permissions(permissions))
    return {
        "roles": sorted(roles),
        "capabilities": capabilities,
        "source": "backend",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _ai_role_allowed(feature: str, roles: set[str]) -> bool:
    if not roles:
        return False
    if feature in {"generation", "admin"}:
        return bool(roles.intersection({"admin", "manager", "partner"}))
    return True


def estimate_ai_tokens(payload: dict[str, Any]) -> int:
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    settings = get_settings()
    if len(raw.encode("utf-8")) > settings.ai_max_prompt_bytes:
        raise HTTPException(status_code=413, detail={"code": "ai_prompt_too_large", "message": "AI request is too large."})
    return max(1, min(settings.ai_max_input_tokens, len(raw) // 4 + 1))


class AIQuotaService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()

    async def reserve(
        self,
        *,
        request: Request,
        user_id: uuid.UUID,
        roles: set[str],
        feature: str,
        model: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> uuid.UUID:
        if not _ai_role_allowed(feature, roles):
            raise HTTPException(status_code=403, detail={"code": "ai_permission_denied", "message": "Permission denied."})
        provider = self.settings.ai_provider_name.strip().lower()
        provider_uses_default_url = "gemini" in provider or "google" in provider
        if not self.settings.resolved_ai_api_key or (not provider_uses_default_url and not self.settings.ai_api_url):
            raise HTTPException(status_code=503, detail={"code": "ai_provider_unconfigured", "message": "AI service is not configured."})
        model_name = model.strip() or self.settings.ai_default_model
        if model_name not in self.settings.ai_model_allowlist_set:
            raise HTTPException(status_code=403, detail={"code": "ai_model_not_allowed", "message": "Permission denied."})
        reserved_tokens = estimate_ai_tokens(payload) + min(self.settings.ai_max_output_tokens, int(payload.get("max_tokens") or self.settings.ai_max_output_tokens))
        estimated_cost = reserved_tokens * self.settings.ai_estimated_cost_per_token
        now = datetime.now(timezone.utc)
        usage_date = now.date().isoformat()
        usage_month = usage_date[:7]
        idem_hash = hmac_digest(f"ai:{user_id}:{idempotency_key}") if idempotency_key else None
        lock_key = _advisory_lock_id(f"ai-quota:{user_id}:{usage_date}:{feature}")
        await self.session.execute(text("select pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_key})
        if idem_hash:
            existing = (
                await self.session.execute(
                    text(
                        """
                        select id
                        from security_events
                        where user_id = :user_id
                          and type = 'ai_usage_ledger'
                          and extra_data->>'idempotency_hash' = :idempotency_hash
                          and deleted_at is null
                        limit 1
                        """
                    ),
                    {"user_id": user_id, "idempotency_hash": idem_hash},
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        counters = (
            await self.session.execute(
                text(
                    """
                    select
                      count(*) filter (where extra_data->>'usage_date' = :usage_date and status in ('reserved','completed')) as daily_requests,
                      count(*) filter (where extra_data->>'usage_month' = :usage_month and status in ('reserved','completed')) as monthly_requests,
                      coalesce(sum(coalesce((extra_data->>'reserved_tokens')::int, 0)) filter (where extra_data->>'usage_date' = :usage_date and status in ('reserved','completed')), 0) as daily_tokens,
                      coalesce(sum(coalesce((extra_data->>'reserved_tokens')::int, 0)) filter (where extra_data->>'usage_month' = :usage_month and status in ('reserved','completed')), 0) as monthly_tokens,
                      coalesce(sum(coalesce((extra_data->>'estimated_cost')::numeric, 0)) filter (where extra_data->>'usage_date' = :usage_date and status in ('reserved','completed')), 0) as daily_cost,
                      coalesce(sum(coalesce((extra_data->>'estimated_cost')::numeric, 0)) filter (where extra_data->>'usage_month' = :usage_month and status in ('reserved','completed')), 0) as monthly_cost,
                      count(*) filter (where status = 'reserved') as concurrent_requests
                    from security_events
                    where user_id = :user_id
                      and type = 'ai_usage_ledger'
                      and deleted_at is null
                    """
                ),
                {"user_id": user_id, "usage_date": usage_date, "usage_month": usage_month},
            )
        ).mappings().one()
        if int(counters["concurrent_requests"] or 0) >= self.settings.ai_max_concurrent_requests_per_user:
            raise HTTPException(status_code=429, detail={"code": "ai_concurrency_limit_exceeded", "message": "AI quota exceeded."})
        if int(counters["daily_requests"] or 0) >= self.settings.ai_daily_request_limit:
            raise HTTPException(status_code=429, detail={"code": "ai_daily_quota_exceeded", "message": "AI quota exceeded."})
        if int(counters["monthly_requests"] or 0) >= self.settings.ai_monthly_request_limit:
            raise HTTPException(status_code=429, detail={"code": "ai_monthly_quota_exceeded", "message": "AI quota exceeded."})
        if int(counters["daily_tokens"] or 0) + reserved_tokens > self.settings.ai_daily_token_limit:
            raise HTTPException(status_code=429, detail={"code": "ai_daily_token_quota_exceeded", "message": "AI quota exceeded."})
        if int(counters["monthly_tokens"] or 0) + reserved_tokens > self.settings.ai_monthly_token_limit:
            raise HTTPException(status_code=429, detail={"code": "ai_monthly_token_quota_exceeded", "message": "AI quota exceeded."})
        if float(counters["daily_cost"] or 0) + estimated_cost > self.settings.ai_daily_cost_limit:
            raise HTTPException(status_code=429, detail={"code": "ai_daily_cost_quota_exceeded", "message": "AI quota exceeded."})
        if float(counters["monthly_cost"] or 0) + estimated_cost > self.settings.ai_monthly_cost_limit:
            raise HTTPException(status_code=429, detail={"code": "ai_monthly_cost_quota_exceeded", "message": "AI quota exceeded."})
        row_id = uuid.uuid4()
        extra = {
            "feature": feature,
            "provider": self.settings.ai_provider_name,
            "model": model_name,
            "request_id": request_id_from_request(request),
            "idempotency_hash": idem_hash,
            "usage_date": usage_date,
            "usage_month": usage_month,
            "reserved_tokens": reserved_tokens,
            "estimated_cost": estimated_cost,
            "currency": self.settings.ai_cost_currency,
        }
        await self.session.execute(
            text(
                """
                insert into security_events (id, user_id, type, status, description, path, extra_data)
                values (:id, :user_id, 'ai_usage_ledger', 'reserved', :description, :path, cast(:extra_data as jsonb))
                """
            ),
            {
                "id": row_id,
                "user_id": user_id,
                "description": hmac_digest(f"ai:{user_id}:{feature}:{row_id}"),
                "path": request.url.path,
                "extra_data": json.dumps(extra, ensure_ascii=False),
            },
        )
        return row_id

    async def complete(self, ledger_id: uuid.UUID, *, actual_tokens: int | None = None, provider_request_id: str | None = None) -> None:
        patch = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "actual_tokens": actual_tokens,
            "provider_request_id_masked": hmac_digest(provider_request_id)[:16] if provider_request_id else None,
        }
        await self.session.execute(
            text(
                """
                update security_events
                set status = 'completed',
                    extra_data = coalesce(extra_data, '{}'::jsonb) || cast(:patch as jsonb)
                where id = :id and type = 'ai_usage_ledger'
                """
            ),
            {"id": ledger_id, "patch": json.dumps({k: v for k, v in patch.items() if v is not None}, ensure_ascii=False)},
        )

    async def fail(self, ledger_id: uuid.UUID, *, error_code_safe: str) -> None:
        patch = {"failed_at": datetime.now(timezone.utc).isoformat(), "error_code_safe": error_code_safe}
        await self.session.execute(
            text(
                """
                update security_events
                set status = 'failed',
                    extra_data = coalesce(extra_data, '{}'::jsonb) || cast(:patch as jsonb)
                where id = :id and type = 'ai_usage_ledger'
                """
            ),
            {"id": ledger_id, "patch": json.dumps(patch, ensure_ascii=False)},
        )


def policy_registry_snapshot() -> list[dict[str, Any]]:
    examples = [
        ("GET", "/health"),
        ("POST", "/auth/login"),
        ("GET", "/api/catalog/products"),
        ("POST", "/storage/upload"),
        ("POST", "/support/tickets"),
        ("POST", "/resources/products/query"),
        ("POST", "/e2e/verify/products"),
        ("POST", "/orders/checkout"),
        ("POST", "/partner/reports/summary"),
        ("POST", "/admin/manual-order"),
        ("POST", "/api/payments/00000000-0000-0000-0000-000000000000/review"),
        ("POST", "/functions/ai-product-assistant"),
        ("POST", "/functions/generate-product-descriptions"),
        ("POST", "/internal/workers/notifications/process"),
        ("GET", "/internal/runtime-fingerprint"),
    ]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for method, path in examples:
        policy = asdict(policy_for_route(method, path))
        if policy["policy_name"] in seen:
            continue
        seen.add(policy["policy_name"])
        rows.append(policy)
    rate_policies = _settings_rate_policies()
    for name in sorted(rate_policies):
        if name in seen:
            continue
        rate_policy = rate_policies[name]
        rows.append(
            asdict(
                ApiProtectionPolicy(
                    policy_name=name,
                    authentication_required=name
                    not in {"public_read", "authentication", "search", "search_anon"},
                    rate_limit_policy=name,
                    rate_limit_keys=rate_policy.key_parts,
                    public_or_private="public"
                    if name in {"public_read", "authentication", "search", "search_anon"}
                    else "private",
                    request_id_required=True,
                    sensitive_response=name
                    not in {"public_read", "search", "search_anon"},
                    ai_quota_policy="generation"
                    if name == "ai_generation"
                    else "recommendation"
                    if name == "ai_recommendation"
                    else None,
                )
            )
        )
    return rows
