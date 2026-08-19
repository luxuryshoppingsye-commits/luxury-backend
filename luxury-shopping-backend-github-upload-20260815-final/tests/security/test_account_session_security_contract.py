from __future__ import annotations

import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.api.routes import auth as auth_routes
from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import AccountSecurity, PasswordResetToken, PhoneOtpToken, Profile, RefreshToken, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing account/session tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing account/session tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if settings.database_name != "luxury_full_cross_platform_e2e_test":
        pytest.fail("Refusing account/session tests outside luxury_full_cross_platform_e2e_test", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing account/session tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing account/session tests on recovery database", pytrace=False)


async def _seed_user(run_id: str, role: str = "customer", *, active: bool = True) -> tuple[User, str]:
    password = "ValidPass123"
    email = f"{run_id}-{role}-{uuid.uuid4().hex[:10]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=active)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"Account Security {role}"))
        session.add(UserRole(user_id=user.id, role=role))
        session.add(AccountSecurity(user_id=user.id, account_status="active" if active else "disabled"))
        await session.commit()
        return user, password


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        "access": body["access_token"],
        "refresh": body["refresh_token"],
        "header": f"Bearer {body['access_token']}",
    }


async def _latest_email_token(user_id: uuid.UUID, purpose_key: str) -> str:
    async with SessionFactory() as session:
        outbox = MODEL_BY_TABLE["email_outbox"]
        rows = (
            await session.execute(
                select(outbox)
                .where(outbox.user_id == user_id)
                .order_by(outbox.created_at.desc())
            )
        ).scalars().all()
        for row in rows:
            extra = row.extra_data or {}
            url = extra.get(purpose_key)
            if url:
                token = parse_qs(urlparse(str(url)).query).get("token", [None])[0]
                if token:
                    return token
    raise AssertionError(f"missing email token for {purpose_key}")


async def _latest_email_verification_code(user_id: uuid.UUID) -> str:
    async with SessionFactory() as session:
        outbox = MODEL_BY_TABLE["email_outbox"]
        rows = (
            await session.execute(
                select(outbox)
                .where(outbox.user_id == user_id)
                .order_by(outbox.created_at.desc())
            )
        ).scalars().all()
        for row in rows:
            url = (row.extra_data or {}).get("verification_url")
            if url:
                code = parse_qs(urlparse(str(url)).query).get("code", [None])[0]
                if code:
                    return code
    raise AssertionError("missing email verification code")


async def test_email_verification_gates_new_customer_and_invalidates_old_links() -> None:
    _assert_safe_database()
    run_id = f"acct-{uuid.uuid4().hex[:8]}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        email = f"{run_id}@example.com"
        register = await client.post(
            "/auth/register-customer",
            json={"email": email, "password": "ValidPass123", "fullName": "New Customer"},
        )
        assert register.status_code == 201, register.text
        body = register.json()
        user_id = uuid.UUID(body["user"]["id"])
        assert body["requires_verification"] is True
        assert body.get("access_token") is None

        blocked_login = await client.post("/auth/login", json={"email": email, "password": "ValidPass123"})
        assert blocked_login.status_code == 403
        assert blocked_login.json()["detail"] == "pending_email_verification"

        old_code = await _latest_email_verification_code(user_id)
        resend = await client.post("/api/auth/resend-verification", json={"email": email})
        assert resend.status_code == 200, resend.text
        latest_code = await _latest_email_verification_code(user_id)
        assert latest_code != old_code

        old_verify = await client.post(
            "/api/auth/verify-email",
            json={"email": email, "code": old_code},
        )
        assert old_verify.status_code == 400

        latest_verify = await client.post(
            "/api/auth/verify-email",
            json={"email": email, "code": latest_code},
        )
        assert latest_verify.status_code == 200
        replay = await client.post(
            "/api/auth/verify-email",
            json={"email": email, "code": latest_code},
        )
        assert replay.status_code == 400

        login = await client.post("/auth/login", json={"email": email, "password": "ValidPass123"})
        assert login.status_code == 200, login.text

        async with SessionFactory() as session:
            state = await session.get(AccountSecurity, user_id)
            assert state is not None
            assert state.account_status == "active"
            assert state.email_verified_at is not None


async def test_registration_captcha_is_enforced_server_side_with_test_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_safe_database()
    run_id = f"captcha-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        auth_routes,
        "get_settings",
        lambda: SimpleNamespace(
            captcha_required=True,
            fixtures_enabled=True,
            registration_rate_limit=200,
            app_public_url="http://testserver",
            smtp_host="",
            smtp_from_email="",
        ),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.post(
            "/auth/register-customer",
            json={"email": f"{run_id}-missing@example.com", "password": "ValidPass123", "fullName": "Captcha Missing"},
        )
        assert missing.status_code == 400
        assert missing.json()["detail"] == "captcha_required"

        accepted = await client.post(
            "/auth/register-customer",
            json={
                "email": f"{run_id}-ok@example.com",
                "password": "ValidPass123",
                "fullName": "Captcha OK",
                "captchaToken": "test-captcha-ok",
            },
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["captcha_status"] == "verified_test_provider"


async def test_password_reset_latest_only_and_invalidates_old_access_and_refresh() -> None:
    _assert_safe_database()
    run_id = f"reset-{uuid.uuid4().hex[:8]}"
    user, password = await _seed_user(run_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await _login(client, user.email, password)

        first = await client.post("/auth/password-reset-request", json={"email": user.email})
        assert first.status_code == 200
        old_token = await _latest_email_token(user.id, "reset_url")
        second = await client.post("/auth/password-reset-request", json={"email": user.email})
        assert second.status_code == 200
        latest_token = await _latest_email_token(user.id, "reset_url")
        assert latest_token != old_token

        old_confirm = await client.post(
            "/auth/password-reset-confirm",
            json={"token": old_token, "newPassword": "NewValidPass123"},
        )
        assert old_confirm.status_code == 400

        latest_confirm = await client.post(
            "/auth/password-reset-confirm",
            json={"token": latest_token, "password": "NewValidPass123"},
        )
        assert latest_confirm.status_code == 200, latest_confirm.text

        stale_access = await client.get("/me", headers={"Authorization": tokens["header"]})
        assert stale_access.status_code == 401
        stale_refresh = await client.post("/auth/refresh", json={"refreshToken": tokens["refresh"]})
        assert stale_refresh.status_code == 401

        replay = await client.post(
            "/auth/password-reset-confirm",
            json={"token": latest_token, "newPassword": "AnotherPass123"},
        )
        assert replay.status_code == 400

        new_login = await client.post("/auth/login", json={"email": user.email, "password": "NewValidPass123"})
        assert new_login.status_code == 200

        async with SessionFactory() as session:
            active_resets = await session.execute(
                select(func.count(PasswordResetToken.id)).where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.used_at.is_(None),
                )
            )
            assert int(active_resets.scalar_one()) <= 1


async def test_refresh_reuse_revokes_session_family_and_sessions_api_is_owner_scoped() -> None:
    _assert_safe_database()
    run_id = f"sess-{uuid.uuid4().hex[:8]}"
    user, password = await _seed_user(run_id)
    other, other_password = await _seed_user(f"{run_id}-other")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await _login(client, user.email, password)
        other_tokens = await _login(client, other.email, other_password)

        rotated = await client.post("/auth/refresh", json={"refreshToken": tokens["refresh"]})
        assert rotated.status_code == 200, rotated.text
        new_refresh = rotated.json()["refresh_token"]

        reuse = await client.post("/auth/refresh", json={"refreshToken": tokens["refresh"]})
        assert reuse.status_code == 401
        assert reuse.json()["detail"] == "refresh_token_reuse_detected"

        family_revoked = await client.post("/auth/refresh", json={"refreshToken": new_refresh})
        assert family_revoked.status_code == 401

        sessions = await client.get("/api/auth/sessions", headers={"Authorization": other_tokens["header"]})
        assert sessions.status_code == 200
        assert sessions.json()["data"]
        session_id = sessions.json()["data"][0]["id"]

        forbidden_revoke = await client.delete(
            f"/api/auth/sessions/{session_id}",
            headers={"Authorization": rotated.json()["access_token"] and f"Bearer {rotated.json()['access_token']}"},
        )
        assert forbidden_revoke.status_code in {401, 404}

        own_revoke = await client.delete(
            f"/api/auth/sessions/{session_id}",
            headers={"Authorization": other_tokens["header"]},
        )
        assert own_revoke.status_code == 200


async def test_profile_validation_and_account_deletion_disable_access() -> None:
    _assert_safe_database()
    run_id = f"me-{uuid.uuid4().hex[:8]}"
    user, password = await _seed_user(run_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await _login(client, user.email, password)
        headers = {"Authorization": tokens["header"]}

        unknown_field = await client.patch("/me", headers=headers, json={"is_active": True})
        assert unknown_field.status_code == 422
        bad_url = await client.patch("/me", headers=headers, json={"avatarUrl": "javascript:alert(1)"})
        assert bad_url.status_code == 422
        good = await client.patch("/me", headers=headers, json={"fullName": "Safe User", "phone": "+967777123456"})
        assert good.status_code == 200, good.text

        deletion = await client.post("/me/account-deletion-request", headers=headers, json={"reason": "test"})
        assert deletion.status_code == 200
        login_after_delete = await client.post("/auth/login", json={"email": user.email, "password": password})
        assert login_after_delete.status_code == 403
        refresh_after_delete = await client.post("/auth/refresh", json={"refreshToken": tokens["refresh"]})
        assert refresh_after_delete.status_code == 401

        async with SessionFactory() as session:
            state = await session.get(AccountSecurity, user.id)
            assert state is not None
            assert state.account_status == "deletion_pending"
            deletion_model = MODEL_BY_TABLE["account_deletion_requests"]
            count = await session.execute(
                select(func.count(deletion_model.id)).where(
                    deletion_model.user_id == user.id,
                    deletion_model.status == "pending",
                    deletion_model.deleted_at.is_(None),
                )
            )
            assert int(count.scalar_one()) == 1


async def test_admin_security_cleanup_revokes_blocked_account_sessions() -> None:
    _assert_safe_database()
    run_id = f"cleanup-{uuid.uuid4().hex[:8]}"
    admin, admin_password = await _seed_user(run_id, role="admin")
    blocked, _password = await _seed_user(f"{run_id}-blocked", active=False)
    refresh_id = uuid.uuid4()
    async with SessionFactory() as session:
        session.add(
            RefreshToken(
                id=refresh_id,
                user_id=blocked.id,
                token_hash=f"cleanup-{refresh_id.hex}",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_tokens = await _login(client, admin.email, admin_password)
        cleanup = await client.post("/api/auth/security/cleanup", headers={"Authorization": admin_tokens["header"]})
        assert cleanup.status_code == 200, cleanup.text
        assert cleanup.json()["revoked_refresh_tokens"] >= 1

    async with SessionFactory() as session:
        token = await session.get(RefreshToken, refresh_id)
        assert token is not None
        assert token.revoked_at is not None


async def test_phone_otp_resend_reuse_and_storage_do_not_persist_raw_code() -> None:
    _assert_safe_database()
    run_id = f"otp-{uuid.uuid4().hex[:8]}"
    user, password = await _seed_user(run_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await _login(client, user.email, password)
        headers = {"Authorization": tokens["header"]}
        phone = "+967777456789"

        first = await client.post("/api/auth/phone/send-otp", headers=headers, json={"phone": phone})
        assert first.status_code == 200, first.text
        first_code = first.json()["test_otp"]
        assert first.json()["delivery_status"] == "queued_test_provider"

        second = await client.post("/api/auth/phone/send-otp", headers=headers, json={"phone": phone})
        assert second.status_code == 200, second.text
        second_code = second.json()["test_otp"]
        assert second_code != first_code

        old_code = await client.post("/api/auth/phone/verify-otp", headers=headers, json={"phone": phone, "otp": first_code})
        assert old_code.status_code == 400

        verified = await client.post("/api/auth/phone/verify-otp", headers=headers, json={"phone": phone, "otp": second_code})
        assert verified.status_code == 200, verified.text
        stale_access = await client.get("/me", headers=headers)
        assert stale_access.status_code == 401
        refreshed_tokens = await _login(client, user.email, password)
        replay = await client.post(
            "/api/auth/phone/verify-otp",
            headers={"Authorization": refreshed_tokens["header"]},
            json={"phone": phone, "otp": second_code},
        )
        assert replay.status_code == 400

        async with SessionFactory() as session:
            state = await session.get(AccountSecurity, user.id)
            assert state is not None
            assert state.phone_verified_at is not None
            active_otps = await session.execute(
                select(func.count(PhoneOtpToken.id)).where(
                    PhoneOtpToken.user_id == user.id,
                    PhoneOtpToken.used_at.is_(None),
                    PhoneOtpToken.invalidated_at.is_(None),
                )
            )
            assert int(active_otps.scalar_one()) == 0
            outbox = MODEL_BY_TABLE["whatsapp_outbox"]
            rows = (
                await session.execute(
                    select(outbox).where(outbox.user_id == user.id).order_by(outbox.created_at.desc())
                )
            ).scalars().all()
            assert rows
            serialized = "\n".join(f"{row.message} {row.extra_data}" for row in rows)
            assert first_code not in serialized
            assert second_code not in serialized


async def test_concurrent_duplicate_customer_registration_creates_one_user_and_returns_conflicts() -> None:
    _assert_safe_database()
    run_id = f"dupe-{uuid.uuid4().hex[:8]}"
    email = f"{run_id}@example.com"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        async def submit() -> int:
            response = await client.post(
                "/auth/register-customer",
                json={"email": email, "password": "ValidPass123", "fullName": "Duplicate Customer"},
            )
            return response.status_code

        statuses = await asyncio.gather(*(submit() for _ in range(5)))
        assert statuses.count(201) == 1
        assert statuses.count(409) == 4
        assert 503 not in statuses

        async with SessionFactory() as session:
            count = await session.execute(select(func.count(User.id)).where(func.lower(User.email) == email))
            assert int(count.scalar_one()) == 1


async def test_merchant_registration_stays_pending_until_admin_review() -> None:
    _assert_safe_database()
    run_id = f"merchant-{uuid.uuid4().hex[:8]}"
    admin, admin_password = await _seed_user(run_id, role="admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        merchant_email = f"{run_id}@merchant-example.com"
        pending = await client.post(
            "/auth/register-merchant",
            json={
                "email": merchant_email,
                "password": "ValidPass123",
                "ownerName": "Merchant Owner",
                "storeName": "Pending Store",
                "phone": "+967777123457",
            },
        )
        assert pending.status_code == 201, pending.text
        assert pending.json()["application_status"] == "pending"
        assert pending.json().get("access_token") is None

        pending_login = await client.post("/auth/login", json={"email": merchant_email, "password": "ValidPass123"})
        assert pending_login.status_code == 403
        assert pending_login.json()["detail"] == "pending_merchant_review"

        async with SessionFactory() as session:
            user = (await session.execute(select(User).where(User.email == merchant_email))).scalar_one()
            state = await session.get(AccountSecurity, user.id)
            assert state is not None
            assert state.account_status == "pending_merchant_review"
            application_model = MODEL_BY_TABLE["partner_applications"]
            application = (
                await session.execute(select(application_model).where(application_model.user_id == user.id))
            ).scalar_one()
            application_id = application.id

        admin_tokens = await _login(client, admin.email, admin_password)
        approved = await client.post(
            f"/admin/partner-applications/{application_id}/review",
            headers={"Authorization": admin_tokens["header"]},
            json={"status": "approved"},
        )
        assert approved.status_code == 200, approved.text

        merchant_login = await client.post("/auth/login", json={"email": merchant_email, "password": "ValidPass123"})
        assert merchant_login.status_code == 200, merchant_login.text
        assert "partner" in merchant_login.json()["roles"]

        rejected_email = f"{run_id}-reject@merchant-example.com"
        rejected = await client.post(
            "/auth/register-merchant",
            json={
                "email": rejected_email,
                "password": "ValidPass123",
                "ownerName": "Rejected Owner",
                "storeName": "Rejected Store",
            },
        )
        assert rejected.status_code == 201
        async with SessionFactory() as session:
            rejected_user = (await session.execute(select(User).where(User.email == rejected_email))).scalar_one()
            application_model = MODEL_BY_TABLE["partner_applications"]
            rejected_application = (
                await session.execute(select(application_model).where(application_model.user_id == rejected_user.id))
            ).scalar_one()
        rejected_review = await client.post(
            f"/admin/partner-applications/{rejected_application.id}/review",
            headers={"Authorization": admin_tokens["header"]},
            json={"status": "rejected", "reason": "المستندات غير مكتملة"},
        )
        assert rejected_review.status_code == 200, rejected_review.text
        rejected_login = await client.post("/auth/login", json={"email": rejected_email, "password": "ValidPass123"})
        assert rejected_login.status_code == 403
        assert rejected_login.json()["detail"] == "merchant_rejected"
