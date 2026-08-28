from __future__ import annotations

import uuid
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import AccountSecurity, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing messaging tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing messaging tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if not settings.database_is_test:
        pytest.fail("Refusing messaging tests outside a trusted test database", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing messaging tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing messaging tests on recovery database", pytrace=False)


async def _seed_user(role: str, run_id: str) -> tuple[User, str]:
    password = "ValidPass123"
    email = f"{run_id}-{role}-{uuid.uuid4().hex[:10]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"Messaging {role}"))
        session.add(UserRole(user_id=user.id, role=role))
        session.add(AccountSecurity(user_id=user.id, account_status="active"))
        await session.commit()
        return user, password


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_legacy_email_and_whatsapp_functions_are_blocked() -> None:
    _assert_safe_database()
    run_id = f"msg-{uuid.uuid4().hex[:8]}"
    user, password = await _seed_user("customer", run_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, user.email, password)
        email_response = await client.post(
            "/functions/send-order-email",
            headers=headers,
            json={"email": "victim@example.com", "subject": "free text", "message": "free text"},
        )
        whatsapp_response = await client.post(
            "/functions/whatsapp-notify",
            headers=headers,
            json={"phone": "+967700000000", "message": "free text"},
        )
    assert email_response.status_code == 410
    assert email_response.json()["detail"] == "arbitrary_message_not_allowed"
    assert whatsapp_response.status_code == 410
    assert whatsapp_response.json()["detail"] == "arbitrary_recipient_not_allowed"


async def test_bulk_notifications_reject_empty_recipients_and_staff_role() -> None:
    _assert_safe_database()
    run_id = f"msg-{uuid.uuid4().hex[:8]}"
    staff, staff_password = await _seed_user("staff", run_id)
    admin, admin_password = await _seed_user("admin", run_id)
    customer, _ = await _seed_user("customer", run_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        staff_headers = await _login(client, staff.email, staff_password)
        admin_headers = await _login(client, admin.email, admin_password)
        denied = await client.post(
            "/notifications/send",
            headers=staff_headers,
            json={"userIds": [str(customer.id)], "title": "T", "message": "M", "type": "message"},
        )
        missing = await client.post(
            "/notifications/send",
            headers=admin_headers,
            json={"title": "T", "message": "M", "type": "message"},
        )
        allowed = await client.post(
            "/notifications/send",
            headers=admin_headers,
            json={"userIds": [str(customer.id)], "title": "T", "message": "M", "type": "message"},
        )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "communication_permission_denied"
    assert missing.status_code == 422
    assert missing.json()["detail"] == "recipients_required"
    assert allowed.status_code == 200
    assert allowed.json()["count"] == 1


async def test_admin_notifications_are_scoped_per_recipient() -> None:
    _assert_safe_database()
    run_id = f"msg-{uuid.uuid4().hex[:8]}"
    admin_a, password_a = await _seed_user("admin", f"{run_id}-a")
    admin_b, password_b = await _seed_user("admin", f"{run_id}-b")
    admin_model = MODEL_BY_TABLE["admin_notifications"]
    async with SessionFactory() as session:
        row_a = admin_model(user_id=admin_a.id, recipient_id=admin_a.id, title="A", message="A", body="A", type="audit", is_read=False, status="new")
        row_b = admin_model(user_id=admin_b.id, recipient_id=admin_b.id, title="B", message="B", body="B", type="audit", is_read=False, status="new")
        session.add_all([row_a, row_b])
        await session.commit()
        row_a_id = row_a.id
        row_b_id = row_b.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers_a = await _login(client, admin_a.email, password_a)
        headers_b = await _login(client, admin_b.email, password_b)
        read_a = await client.patch(f"/api/notifications/admin/{row_a_id}/read", headers=headers_a)
        read_b_as_a = await client.patch(f"/api/notifications/admin/{row_b_id}/read", headers=headers_a)
        delete_a = await client.delete(f"/api/notifications/admin/{row_a_id}", headers=headers_a)
        list_b = await client.get("/api/notifications/admin", headers=headers_b)
    assert read_a.status_code == 200
    assert read_b_as_a.status_code == 404
    assert read_b_as_a.json()["detail"] == "notification_recipient_mismatch"
    assert delete_a.status_code == 200
    assert any(item["id"] == str(row_b_id) and not item["is_read"] for item in list_b.json()["data"])


async def test_device_token_is_reassigned_without_duplicate_active_rows() -> None:
    _assert_safe_database()
    run_id = f"msg-{uuid.uuid4().hex[:8]}"
    user_a, password_a = await _seed_user("customer", f"{run_id}-a")
    user_b, password_b = await _seed_user("customer", f"{run_id}-b")
    token = f"fcm-{uuid.uuid4().hex}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers_a = await _login(client, user_a.email, password_a)
        headers_b = await _login(client, user_b.email, password_b)
        first = await client.post("/notifications/device-token", headers=headers_a, json={"token": token, "platform": "android", "deviceId": "device-1"})
        second = await client.post("/notifications/device-token", headers=headers_b, json={"token": token, "platform": "android", "deviceId": "device-1"})
    assert first.status_code == 200
    assert second.status_code == 200
    token_model = MODEL_BY_TABLE["push_tokens"]
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                token_model.__table__.select().where(token_model.__table__.c.token == token, token_model.__table__.c.is_active.is_(True))
            )
        ).mappings().all()
    assert len(rows) == 1
    assert str(rows[0]["user_id"]) == str(user_b.id)


@pytest.mark.parametrize(
    "table,operation",
    [
        ("push_tokens", "select"),
        ("push_tokens", "insert"),
        ("web_push_subscriptions", "select"),
        ("notification_outbox", "insert"),
        ("email_outbox", "update"),
        ("whatsapp_outbox", "delete"),
        ("notification_delivery_attempts", "select"),
    ],
)
async def test_generic_resource_messaging_bypass_is_denied(table: str, operation: str) -> None:
    _assert_safe_database()
    run_id = f"msg-{uuid.uuid4().hex[:8]}"
    admin, password = await _seed_user("admin", run_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin.email, password)
        response = await client.post(
            f"/resources/{table}/query",
            headers=headers,
            json={"operation": operation, "data": {"status": "sent"}, "filters": [], "limit": 1},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == f"generic_messaging_resource_bypass_denied:{table}"
