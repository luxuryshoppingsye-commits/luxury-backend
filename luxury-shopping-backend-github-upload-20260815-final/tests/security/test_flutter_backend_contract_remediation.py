from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from backend.app.config import get_settings
from backend.app.main import app, integrity_exception_handler
from backend.app.models.domain import User
from backend.app.services import payment_refund_security


pytestmark = pytest.mark.asyncio


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/contract-test", "headers": []})


def _assert_safe_write_runtime() -> None:
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.allow_test_fixtures is True
    assert "test" in settings.database_name.lower()
    assert settings.database_name.lower() != "luxury_official_recovery"


async def test_password_policy_endpoint_is_backend_source_of_truth() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/api/auth/password-policy")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["min_length"] == 8
    assert payload["max_length"] >= 8
    assert payload["require_letter"] is True
    assert payload["require_number"] is True


async def test_password_reset_rejects_open_redirect_before_side_effects() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/api/auth/forgot-password",
            json={
                "email": "contract-reset@example.com",
                "redirect_to": "javascript:alert(1)",
                "client_type": "flutter",
            },
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "invalid_redirect_url"
    assert payload["detail"] == "invalid_redirect_url"


async def test_concurrent_duplicate_registration_returns_single_success_and_conflict() -> None:
    _assert_safe_write_runtime()
    email = f"contract-concurrent-{uuid.uuid4().hex}@example.com"
    payload = {
        "email": email,
        "password": "Contract123",
        "fullName": "Contract Concurrent",
        "phone": "700000009",
        "city": "Sanaa",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        responses = await asyncio.gather(
            client.post("/auth/register-customer", json=payload),
            client.post("/auth/register-customer", json=payload),
        )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    body = conflict.json()
    assert body["http_status"] == 409
    assert body["error"]["code"] == "email_exists"
    raw = conflict.text
    assert "23505" not in raw
    assert "SQLSTATE" not in raw


async def test_http_error_contract_keeps_status_separate_from_application_code() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.patch("/api/auth/change-password", json={})

    assert response.status_code == 401
    payload = response.json()
    assert payload["http_status"] == 401
    assert payload["error"]["code"] == "authentication_required"
    assert "request_id" in payload["error"]


async def test_unique_violation_maps_to_409_without_raw_sqlstate_leak() -> None:
    class _Orig:
        sqlstate = "23505"
        constraint_name = "users_email_key"

        def __str__(self) -> str:
            return "duplicate key value violates unique constraint users_email_key"

    response = await integrity_exception_handler(
        _request(),
        IntegrityError("insert", {}, _Orig()),
    )

    assert response.status_code == 409
    body = response.body.decode("utf-8")
    assert "23505" not in body
    assert "users_email_key" not in body
    assert "duplicate_record" in body or "email_exists" in body


async def test_signed_receipt_url_honors_requested_expiry(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    storage_root = tmp_path / "uploads"
    receipt_path = storage_root / "_private" / "receipts" / "proof.png"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(b"\x89PNG\r\n\x1a\ncontract")
    receipt_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), email="receipt-owner@example.com", password_hash="hash", is_active=True)
    row = SimpleNamespace(id=receipt_id)

    async def _fake_find(*_args, **_kwargs):
        return row

    monkeypatch.setattr(payment_refund_security, "find_receipt_for_access", _fake_find)
    monkeypatch.setattr(payment_refund_security, "_receipt_storage_path", lambda *_args, **_kwargs: receipt_path)
    session = SimpleNamespace(add=lambda *_args, **_kwargs: None, commit=lambda: None)

    async def _commit():
        return None

    session.commit = _commit
    response = await payment_refund_security.issue_signed_receipt_url(
        session,
        request=Request({"type": "http", "method": "POST", "path": "/receipts/signed-url", "headers": [(b"host", b"testserver")]}),
        receipt_ref=str(receipt_id),
        user=user,
        roles={"customer"},
        storage=SimpleNamespace(root=storage_root),
        expires_in=60,
    )

    assert response["expires_in_effective"] == 60
    assert "/receipts/access?token=" in response["signed_url"]
