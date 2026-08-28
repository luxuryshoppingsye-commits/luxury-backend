from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import text
from starlette.requests import Request

from app.config import Settings, get_settings
from app.database import SessionFactory
from app.main import app
from app.models.domain import AccountSecurity, Profile, User, UserRole
from app.security.passwords import hash_password
from app.services.api_protection import (
    REQUEST_ID_HEADER,
    capabilities_for_roles,
    policy_registry_snapshot,
    policy_for_route,
    route_policy_coverage,
    sanitize_request_id,
    trusted_client_ip,
)


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing API protection tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing API protection tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if not settings.database_is_test:
        pytest.fail("Refusing API protection tests outside a trusted test database", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing API protection tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing API protection tests on recovery database", pytrace=False)


async def _seed_user(role: str, run_id: str) -> tuple[str, str]:
    password = "ValidPass123"
    email = f"{run_id}-{role}-{uuid.uuid4().hex[:10]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"API Protection {role}"))
        session.add(UserRole(user_id=user.id, role=role))
        session.add(AccountSecurity(user_id=user.id, account_status="active"))
        await session.commit()
    return email, password


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_production_cors_rejects_wildcard_and_local_origins() -> None:
    base = {
        "DATABASE_URL": "postgresql://user@db.example.com:5432/luxury_operational",
        "APP_ENV": "production",
        "ALLOW_TEST_FIXTURES": False,
        "JWT_SECRET": "unit-test-jwt-secret-512512512512512512",
        "API_BASE_URL": "https://api.luxuryshoppings.com",
        "APP_PUBLIC_URL": "https://api.luxuryshoppings.com",
        "WS_BASE_URL": "wss://api.luxuryshoppings.com",
        "FRONTEND_PUBLIC_URL": "https://luxuryshoppings.com",
        "STORAGE_PROVIDER": "r2",
        "R2_ENDPOINT_URL": "https://r2.example.com",
        "R2_BUCKET": "luxury-assets",
        "R2_ACCESS_KEY_ID": "test-access-key",
        "R2_SECRET_ACCESS_KEY": "test-secret-key",
        "R2_PUBLIC_BASE_URL": "https://assets.example.com",
        "BACKUP_OFFSITE_PROVIDER": "s3",
        "BACKUP_S3_BUCKET": "luxury-prod-backups",
    }
    with pytest.raises(ValidationError, match="cannot contain '\\*'"):
        Settings(**base, CORS_ORIGINS="*")
    with pytest.raises(ValidationError, match="localhost or emulator"):
        Settings(**base, CORS_ORIGINS="http://127.0.0.1:5190")
    settings = Settings(**base, CORS_ORIGINS="https://luxuryshoppings.com,https://www.luxuryshoppings.com")
    assert settings.allowed_origins == ["https://luxuryshoppings.com", "https://www.luxuryshoppings.com"]


def test_production_default_backup_provider_does_not_block_web_startup() -> None:
    base = {
        "DATABASE_URL": "postgresql://user@db.example.com:5432/luxury_operational",
        "APP_ENV": "production",
        "ALLOW_TEST_FIXTURES": False,
        "JWT_SECRET": "unit-test-jwt-secret-512512512512512512",
        "API_BASE_URL": "https://api.luxuryshoppings.com",
        "APP_PUBLIC_URL": "https://api.luxuryshoppings.com",
        "WS_BASE_URL": "wss://api.luxuryshoppings.com",
        "FRONTEND_PUBLIC_URL": "https://luxuryshoppings.com",
        "STORAGE_PROVIDER": "r2",
        "R2_ENDPOINT_URL": "https://r2.example.com",
        "R2_BUCKET": "luxury-assets",
        "R2_ACCESS_KEY_ID": "test-access-key",
        "R2_SECRET_ACCESS_KEY": "test-secret-key",
        "R2_PUBLIC_BASE_URL": "https://assets.example.com",
        "CORS_ORIGINS": "https://luxuryshoppings.com,https://www.luxuryshoppings.com",
    }
    settings = Settings(**base)
    # Production filesystem backup settings are promoted to the configured
    # S3-compatible R2 target so backups survive ephemeral Render storage.
    assert settings.backup_offsite_provider == "s3"
    explicit_filesystem = Settings(**base, BACKUP_OFFSITE_PROVIDER="filesystem")
    assert explicit_filesystem.backup_offsite_provider == "s3"


def test_production_s3_backup_provider_reuses_r2_credentials_when_dedicated_values_are_omitted() -> None:
    base = {
        "DATABASE_URL": "postgresql://user@db.example.com:5432/luxury_operational",
        "APP_ENV": "production",
        "ALLOW_TEST_FIXTURES": False,
        "JWT_SECRET": "unit-test-jwt-secret-512512512512512512",
        "API_BASE_URL": "https://api.luxuryshoppings.com",
        "APP_PUBLIC_URL": "https://api.luxuryshoppings.com",
        "WS_BASE_URL": "wss://api.luxuryshoppings.com",
        "FRONTEND_PUBLIC_URL": "https://luxuryshoppings.com",
        "STORAGE_PROVIDER": "r2",
        "R2_ENDPOINT_URL": "https://r2.example.com",
        "R2_BUCKET": "luxury-assets",
        "R2_ACCESS_KEY_ID": "test-access-key",
        "R2_SECRET_ACCESS_KEY": "test-secret-key",
        "R2_REGION": "auto",
        "R2_PUBLIC_BASE_URL": "https://assets.example.com",
        "BACKUP_OFFSITE_PROVIDER": "s3",
        "CORS_ORIGINS": "https://luxuryshoppings.com,https://www.luxuryshoppings.com",
    }
    settings = Settings(**base)
    assert settings.backup_s3_bucket == "luxury-assets"
    assert settings.backup_s3_endpoint_url == "https://r2.example.com"
    assert settings.backup_s3_access_key_id == "test-access-key"


def test_route_policy_registry_classifies_registered_routes() -> None:
    _assert_safe_database()
    coverage = route_policy_coverage(app)
    assert coverage["total_routes"] > 0
    assert coverage["unclassified_routes"] == 0
    assert policy_for_route("POST", "/functions/ai-product-assistant").authentication_required is True
    assert policy_for_route("POST", "/functions/get_product_likes_count").authentication_required is False
    assert policy_for_route("GET", "/api/orders").authentication_required is True
    assert policy_for_route("GET", "/notifications/outbox").policy_name == "admin_write"
    assert policy_for_route("GET", "/api/marketing/campaigns/active").authentication_required is False
    assert capabilities_for_roles({"customer"})["capabilities"]["ai:use"] is True
    assert capabilities_for_roles({"customer"})["capabilities"]["ai:generate_content"] is False
    assert capabilities_for_roles({"admin"})["capabilities"]["ai:generate_content"] is True
    registry_names = {row["policy_name"] for row in policy_registry_snapshot()}
    assert {
        "public_read",
        "authentication",
        "search",
        "search_anon",
        "upload",
        "support_write",
        "generic_resource",
        "customer_write",
        "merchant_write",
        "admin_write",
        "finance_write",
        "ai_generation",
        "ai_recommendation",
        "internal_worker",
        "internal_diagnostics",
    }.issubset(registry_names)


def test_frontend_clients_do_not_call_sensitive_internal_functions() -> None:
    project_root = Path(__file__).resolve().parents[3]
    search_roots = [
        project_root / "luxury-shopping-handover-20260609" / "src",
        project_root / "lib",
    ]
    forbidden_terms = {
        "check_login_rate_limit",
        "check_password_reset_rate_limit",
        "is_identity_banned",
        "increment_coupon_usage",
        "redeem_loyalty_points",
        "provider_error",
    }
    findings: list[str] = []
    for root in search_roots:
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".dart", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            text_value = path.read_text(encoding="utf-8", errors="ignore")
            for term in forbidden_terms:
                if term in text_value:
                    findings.append(f"{path.relative_to(project_root)} contains {term}")
    assert findings == []


def test_trusted_proxy_ip_extraction_rejects_spoofed_forwarded_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    get_settings.cache_clear()
    try:
        untrusted = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/health",
                "headers": [(b"x-forwarded-for", b"198.51.100.20")],
                "client": ("203.0.113.10", 43111),
            }
        )
        trusted = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/health",
                "headers": [(b"x-forwarded-for", b"198.51.100.20")],
                "client": ("10.0.0.10", 43111),
            }
        )
        assert trusted_client_ip(untrusted) == "203.0.113.10"
        assert trusted_client_ip(trusted) == "198.51.100.20"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio(loop_scope="module")
async def test_request_id_security_headers_and_postgresql_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_safe_database()
    monkeypatch.setenv("PUBLIC_READ_RATE_LIMIT", "1")
    get_settings.cache_clear()
    try:
        path = f"/api/uploads/api-protection-rate-{uuid.uuid4().hex}.txt"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            first = await client.get(path, headers={REQUEST_ID_HEADER: "bad request id"})
            second = await client.get(path, headers={REQUEST_ID_HEADER: "bad request id"})
        assert first.status_code == 404
        assert first.headers[REQUEST_ID_HEADER] != "bad request id"
        assert first.headers["x-content-type-options"] == "nosniff"
        assert first.headers["x-frame-options"] == "DENY"
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limit_exceeded"
        assert int(second.headers["Retry-After"]) >= 1
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio(loop_scope="module")
async def test_private_routes_require_authentication_before_handler() -> None:
    _assert_safe_database()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/admin/manual-order", json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers[REQUEST_ID_HEADER]


@pytest.mark.asyncio(loop_scope="module")
async def test_ai_functions_require_auth_and_return_safe_provider_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_safe_database()
    monkeypatch.setenv("AI_API_URL", "")
    monkeypatch.setenv("AI_API_KEY", "")
    get_settings.cache_clear()
    run_id = f"api-protect-{uuid.uuid4().hex[:8]}"
    email, password = await _seed_user("customer", run_id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            anonymous = await client.post("/functions/ai-product-assistant", json={"question": "hello"})
            headers = await _login(client, email, password)
            configured = await client.post(
                "/functions/ai-product-assistant",
                headers=headers,
                json={"question": "hello", "provider": "client-selected-provider"},
            )
            unavailable = await client.post(
                "/functions/ai-product-assistant",
                headers=headers,
                json={"question": "hello"},
            )
        assert anonymous.status_code == 401
        assert configured.status_code == 403
        assert configured.json()["error"]["code"] == "ai_provider_selection_denied"
        assert unavailable.status_code == 503
        payload = unavailable.json()
        assert payload["error"]["code"] == "ai_provider_unconfigured"
        assert "provider_error" not in str(payload)
        assert "AI_API_KEY" not in str(payload)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio(loop_scope="module")
async def test_sensitive_function_enumeration_and_direct_financial_mutators_are_blocked() -> None:
    _assert_safe_database()
    run_id = f"api-protect-{uuid.uuid4().hex[:8]}"
    customer_email, customer_password = await _seed_user("customer", run_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        customer_headers = await _login(client, customer_email, customer_password)
        rate_probe = await client.post(
            "/functions/check_login_rate_limit",
            headers=customer_headers,
            json={"email": "victim@example.com"},
        )
        coupon_direct = await client.post(
            "/functions/increment_coupon_usage",
            headers=customer_headers,
            json={"coupon_id": str(uuid.uuid4())},
        )
        loyalty_direct = await client.post(
            "/functions/redeem_loyalty_points",
            headers=customer_headers,
            json={"points": 1},
        )
    assert rate_probe.status_code == 403
    assert coupon_direct.status_code == 410
    assert coupon_direct.json()["error"]["code"] == "coupon_usage_checkout_only"
    assert loyalty_direct.status_code == 410
    assert loyalty_direct.json()["error"]["code"] == "loyalty_redeem_checkout_only"


@pytest.mark.asyncio(loop_scope="module")
async def test_generic_resource_public_select_is_bounded_and_anonymous_mutation_is_blocked() -> None:
    _assert_safe_database()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        unbounded = await client.post(
            "/resources/products/query",
            json={
                "operation": "select",
                "filters": [{"column": "name", "operator": "ilike", "value": "%"}],
            },
        )
        bounded = await client.post(
            "/resources/products/query",
            json={"operation": "select", "limit": 5000, "count": True},
        )
        mutation = await client.post(
            "/resources/wishlist/query",
            json={"operation": "insert", "data": {"product_id": str(uuid.uuid4())}},
        )
        admin_content = await client.get("/api/content/theme?admin=true")
    assert unbounded.status_code == 422
    assert unbounded.json()["error"]["code"] == "unbounded_wildcard_filter_denied"
    assert bounded.status_code == 200
    assert bounded.json()["page_size"] <= get_settings().resource_max_page_size
    assert "total" in bounded.json()
    assert mutation.status_code == 401
    assert admin_content.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_api_rate_limit_and_ai_usage_ledgers_do_not_store_plain_tokens_or_urls() -> None:
    _assert_safe_database()
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    select coalesce(description, '') as description, coalesce(extra_data::text, '') as extra_data
                    from security_events
                    where type in ('api_rate_limit', 'ai_usage_ledger')
                    order by created_at desc
                    limit 50
                    """
                )
            )
        ).mappings().all()
    joined = "\n".join(f"{row['description']} {row['extra_data']}" for row in rows)
    assert "postgresql://" not in joined
    assert "Bearer " not in joined
    assert "AI_API_KEY" not in joined
