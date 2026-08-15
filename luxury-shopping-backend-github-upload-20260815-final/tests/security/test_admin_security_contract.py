from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models.domain import LoginAttempt, Profile, RefreshToken, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio(loop_scope="module")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(email: str, role: str, full_name: str, *, is_active: bool = True) -> str:
    password = "ValidPass123"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=is_active)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=full_name))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
    return password


async def _login(client: AsyncClient, email: str, password: str) -> dict:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


async def test_admin_routes_reject_missing_customer_expired_and_inactive_sessions() -> None:
    suffix = uuid.uuid4().hex[:10]
    admin_email = f"admin-security-{suffix}@example.com"
    customer_email = f"customer-security-{suffix}@example.com"
    inactive_email = f"inactive-security-{suffix}@example.com"

    admin_password = await _seed_user(admin_email, "admin", "Admin Security")
    customer_password = await _seed_user(customer_email, "customer", "Customer Security")
    inactive_password = await _seed_user(inactive_email, "admin", "Inactive Admin", is_active=False)

    async with SessionFactory() as session:
        before_attempts = int(
            (await session.execute(select(func.count()).select_from(LoginAttempt))).scalar_one()
        )
        before_tokens = int(
            (await session.execute(select(func.count()).select_from(RefreshToken))).scalar_one()
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.get("/admin/customers")
        assert missing.status_code == 401

        customer = await _login(client, customer_email, customer_password)
        forbidden = await client.get("/admin/customers", headers=_headers(customer["access_token"]))
        assert forbidden.status_code == 403

        inactive = await client.post("/auth/login", json={"email": inactive_email, "password": inactive_password})
        assert inactive.status_code == 401

        admin = await _login(client, admin_email, admin_password)
        allowed = await client.get("/admin/customers", headers=_headers(admin["access_token"]))
        assert allowed.status_code == 200

        expired = jwt.encode(
            {
                "sub": admin["user"]["id"],
                "roles": admin["roles"],
                "type": "access",
                "iat": datetime.now(timezone.utc) - timedelta(hours=2),
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            get_settings().jwt_secret,
            algorithm="HS256",
        )
        expired_response = await client.get("/admin/customers", headers=_headers(expired))
        assert expired_response.status_code == 401

        refreshed = await client.post("/auth/refresh", json={"refreshToken": admin["refresh_token"]})
        assert refreshed.status_code == 200

        logout = await client.post("/auth/logout", json={"refreshToken": refreshed.json()["refresh_token"]})
        assert logout.status_code == 200

        replay_refresh = await client.post(
            "/auth/refresh",
            json={"refreshToken": refreshed.json()["refresh_token"]},
        )
        assert replay_refresh.status_code == 401

    async with SessionFactory() as session:
        after_attempts = int(
            (await session.execute(select(func.count()).select_from(LoginAttempt))).scalar_one()
        )
        after_tokens = int(
            (await session.execute(select(func.count()).select_from(RefreshToken))).scalar_one()
        )
        assert after_attempts >= before_attempts + 3
        assert after_tokens >= before_tokens + 3

