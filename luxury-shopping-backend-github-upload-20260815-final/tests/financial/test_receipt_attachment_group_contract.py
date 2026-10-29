from __future__ import annotations

import io
import uuid
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import FileAsset, Order, Profile, User, UserRole
from backend.app.security.passwords import hash_password
from backend.app.services.financial_calculator import approved_payment_total


@pytest.mark.asyncio
async def test_multiple_images_are_one_payment_with_private_previews() -> None:
    settings = get_settings()
    assert settings.is_test_environment and settings.allow_test_fixtures
    assert urlsplit(settings.database_url).hostname == "127.0.0.1"
    suffix = uuid.uuid4().hex[:8]
    users = []
    async with SessionFactory() as session:
        for name, role in (("ghaya", "customer"), ("sara", "admin"), ("reem", "customer")):
            email = f"{name}-{suffix}@gmail.com"
            user = User(email=email, password_hash=hash_password("Admin1234"), is_active=True)
            session.add(user)
            await session.flush()
            session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=name))
            session.add(UserRole(user_id=user.id, role=role))
            users.append(user)
        order = Order(order_number=f"INV-{suffix.upper()}", user_id=users[0].id,
                      created_by=users[0].id, status="pending", total=Decimal("7000.00"),
                      subtotal=Decimal("7000.00"), discount_total=0, shipping_total=0,
                      currency_code="YER", payment_method="wallet_transfer", payment_status="pending")
        session.add(order)
        await session.flush()
        session.add(MODEL_BY_TABLE["order_payments"](order_id=order.id, status="pending",
                    type="wallet_transfer", amount=Decimal("7000.00")))
        await session.commit()
    image = io.BytesIO()
    Image.new("RGB", (120, 180), "white").save(image, format="PNG")
    files = [("file", (f"transfer-{index}.png", image.getvalue(), "image/png")) for index in (1, 2)]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = []
        for user in users:
            response = await client.post("/auth/login", json={"email": user.email, "password": "Admin1234"})
            assert response.status_code == 200, response.text
            headers.append({"Authorization": f"Bearer {response.json()['access_token']}"})
        uploaded = await client.post(f"/orders/{order.id}/payment-receipt", headers=headers[0],
                                    files=files, data={"amount": "7000", "paymentMethod": "wallet_transfer"})
        assert uploaded.status_code == 201, uploaded.text
        payload = uploaded.json()
        references = payload["receipt_urls"]
        assert len(references) == 2 and len(set(references)) == 2
        assert references[0] == f"receipt:{payload['id']}"
        assert payload["amount"] == "7000.00"
        async with SessionFactory() as session:
            receipts = list((await session.execute(select(MODEL_BY_TABLE["payment_receipts"]).where(
                MODEL_BY_TABLE["payment_receipts"].order_id == order.id))).scalars())
            assets = list((await session.execute(select(FileAsset).where(FileAsset.entity_id == receipts[0].id))).scalars())
            assert len(receipts) == 1 and len(assets) == 2
            assert await approved_payment_total(session, order.id) == Decimal("0.00")
        payments = await client.get(f"/api/payments/orders/{order.id}", headers=headers[1])
        assert payments.status_code == 200, payments.text
        assert payments.json()["data"][0]["receipt_urls"] == references
        detail = await client.get(f"/orders/{order.id}", headers=headers[0])
        assert detail.status_code == 200, detail.text
        assert detail.json()["receipt_urls"] == references
        appended = await client.post(f"/orders/{order.id}/payment-receipt", headers=headers[0],
            files=[files[0]], data={"amount": "7000", "paymentMethod": "wallet_transfer"})
        assert appended.status_code == 201, appended.text
        assert appended.json()["id"] == payload["id"]
        assert appended.json()["receipt_urls"][:2] == references
        references = appended.json()["receipt_urls"]
        assert len(references) == 3
        payments = await client.get(f"/api/payments/orders/{order.id}", headers=headers[1])
        assert payments.json()["data"][0]["receipt_urls"] == references
        async with SessionFactory() as session:
            receipt_rows = list((await session.execute(select(MODEL_BY_TABLE["payment_receipts"]).where(
                MODEL_BY_TABLE["payment_receipts"].order_id == order.id))).scalars())
            assert len(receipt_rows) == 1 and receipt_rows[0].amount == Decimal("7000.00")
        for reference in references:
            for allowed in headers[:2]:
                signed = await client.post("/receipts/signed-url", headers=allowed, json={"receiptPath": reference})
                assert signed.status_code == 200, signed.text
            denied = await client.post("/receipts/signed-url", headers=headers[2], json={"receiptPath": reference})
            assert denied.status_code in (403, 404), denied.text
        invalid = await client.post(f"/orders/{order.id}/payment-receipt", headers=headers[0],
            files=files + [("file", ("transfer.html", b"<html>receipt</html>", "text/html"))], data={"amount": "7000"})
        assert invalid.status_code in (409, 415, 422), invalid.text
        excessive = await client.post(f"/orders/{order.id}/payment-receipt", headers=headers[0], files=files * 6)
        assert excessive.status_code == 422, excessive.text

        accumulated_limit = await client.post(f"/orders/{order.id}/payment-receipt", headers=headers[0],
            files=files * 4, data={"amount": "7000", "paymentMethod": "wallet_transfer"})
        assert accumulated_limit.status_code == 422, accumulated_limit.text

        reviewed = await client.post(f"/payments/{payload['id']}/review", headers=headers[1],
                                     json={"approved": True, "reason": "تمت مراجعة صور التحويل"})
        assert reviewed.status_code == 200, reviewed.text
        await client.post(f"/payments/{payload['id']}/review", headers=headers[1], json={"approved": True})
        async with SessionFactory() as session:
            assert await approved_payment_total(session, order.id) == Decimal("7000.00")
