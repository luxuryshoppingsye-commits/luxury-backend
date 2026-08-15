from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Profile, User, UserRole
from backend.app.security.passwords import hash_password
from backend.app.services import outbox_service


pytestmark = pytest.mark.asyncio


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_email_process_endpoint_is_disabled_and_worker_blocks_unconfigured_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    suffix = uuid.uuid4().hex[:10]
    email = f"codex-finance-admin-{suffix}@example.com"
    password = "ValidPass123"

    async with SessionFactory() as session:
        admin = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(admin)
        await session.flush()
        session.add(Profile(id=admin.id, user_id=admin.id, email=email, full_name="Failure Recovery Admin"))
        session.add(UserRole(user_id=admin.id, role="admin"))
        email_outbox = MODEL_BY_TABLE["email_outbox"]
        session.add(
            email_outbox(
                user_id=admin.id,
                title=f"CODEX_FAILURE_RECOVERY_TEST_email_{suffix}",
                status="pending",
                email="customer@example.com",
                message="Queued locally; external delivery is not required for this test.",
            )
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        response = await client.post("/email/process", headers=_headers(login.json()["access_token"]))

    assert response.status_code == 410, response.text
    assert response.json()["detail"] == "manual_worker_invocation_disabled"

    monkeypatch.setattr(
        outbox_service,
        "get_settings",
        lambda: SimpleNamespace(
            message_batch_size=50,
            message_lock_timeout_seconds=60,
            smtp_host="",
            smtp_username="",
            smtp_password="",
            smtp_from_email="",
        ),
    )
    async with SessionFactory() as session:
        payload = await outbox_service.process_email_outbox(session, limit=10)
        await session.commit()
    assert payload["configured"] is False
    assert payload["blocked_configuration"] >= 1
