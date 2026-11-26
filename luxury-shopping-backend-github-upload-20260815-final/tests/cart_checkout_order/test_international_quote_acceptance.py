from decimal import Decimal
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Profile, User, UserRole
from backend.app.security.passwords import hash_password


@pytest.mark.asyncio
async def test_only_owner_can_accept_current_international_quote_without_payment():
    settings = get_settings()
    assert settings.fixtures_enabled
    suffix = uuid.uuid4().hex[:12]
    password = f"Quote-{suffix}-123"
    version = "2026-11-26T07:00:00+00:00"
    emails = [f"quote-{suffix}-{i}@example.com" for i in range(2)]
    order_model = MODEL_BY_TABLE["international_orders"]
    payment_model = MODEL_BY_TABLE["international_order_payments"]
    async with SessionFactory() as session:
        users = []
        for email, name in zip(emails, ["ليلى عبدالله", "سالم أحمد"]):
            user = User(email=email, password_hash=hash_password(password), is_active=True)
            session.add(user)
            await session.flush()
            session.add_all([
                Profile(id=user.id, user_id=user.id, email=email, full_name=name),
                UserRole(user_id=user.id, role="customer"),
            ])
            users.append(user)
        order = order_model(
            order_number=f"INL-TEST-{suffix}",
            user_id=users[0].id,
            description="عرض قميص ساتان",
            status="reviewing",
            amount=Decimal("20.00"),
            extra_data={
                "pricing_updated_at": version,
                "currency_code": "USD",
                "final_cost": "20.00",
                "payment_status": "unpaid",
            },
        )
        session.add(order)
        await session.flush()
        order_id = order.id
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = []
        for email in emails:
            login = await client.post("/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200
            headers.append({"Authorization": f"Bearer {login.json()['access_token']}"})
        owner, other = headers
        detail = f"/api/orders/international-shopping/{order_id}"
        accept = detail + "/accept-quote"
        before = await client.get(detail, headers=owner)
        assert before.json()["data"]["can_accept_quote"] is True
        assert (await client.post(accept, headers=other, json={"pricing_updated_at": version})).status_code == 404
        assert (await client.post(accept, headers=owner, json={})).status_code == 422
        stale = await client.post(accept, headers=owner, json={"pricing_updated_at": "old"})
        assert stale.status_code == 409
        assert stale.json()["detail"] == "international_quote_changed"

        accepted = await client.post(accept, headers=owner, json={"pricing_updated_at": version})
        assert accepted.status_code == 200, accepted.text
        data = accepted.json()["data"]
        assert data["status"] == "waiting_customer_payment"
        assert data["payment_status"] == "unpaid"
        assert Decimal(data["paid_amount"]) == 0
        assert Decimal(data["remaining_balance"]) == Decimal("20.00")
        assert data["can_accept_quote"] is False
        retry = await client.post(accept, headers=owner, json={"pricing_updated_at": version})
        assert retry.json()["data"]["quote_accepted_at"] == data["quote_accepted_at"]
        reloaded = await client.get(detail, headers=owner)
        assert reloaded.json()["data"]["status"] == data["status"]

    async with SessionFactory() as session:
        payments = await session.scalar(
            select(func.count()).select_from(payment_model).where(payment_model.order_id == order_id)
        )
        assert payments == 0
