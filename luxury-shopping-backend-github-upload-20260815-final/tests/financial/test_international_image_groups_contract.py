from __future__ import annotations

import io
import uuid
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import FileAsset, Profile, User, UserRole
from backend.app.security.passwords import hash_password


@pytest.mark.asyncio
async def test_international_images_and_receipt_group_survive_database_roundtrip():
    settings = get_settings()
    assert settings.is_test_environment and settings.allow_test_fixtures
    assert urlsplit(settings.database_url).hostname == "127.0.0.1"
    suffix = uuid.uuid4().hex[:8]
    users = []
    async with SessionFactory() as session:
        for name, role in (("ghaya", "customer"), ("sara", "admin"), ("reem", "customer")):
            user = User(email=f"{name}-{suffix}@gmail.com", password_hash=hash_password("Admin1234"), is_active=True)
            session.add(user)
            await session.flush()
            session.add(Profile(id=user.id, user_id=user.id, email=user.email, full_name=name))
            session.add(UserRole(user_id=user.id, role=role))
            users.append(user)
        await session.commit()
    image = io.BytesIO()
    Image.new("RGB", (120, 180), "peru").save(image, format="PNG")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = []
        for user in users:
            login = await client.post("/auth/login", json={"email": user.email, "password": "Admin1234"})
            assert login.status_code == 200, login.text
            headers.append({"Authorization": f"Bearer {login.json()['access_token']}"})
        refs = []
        for index in range(3):
            upload = await client.post("/storage/upload", headers=headers[0], files={"file": (f"dress-{index}.png", image.getvalue(), "image/png")}, data={"purpose": "customer_request_attachment"})
            assert upload.status_code == 201, upload.text
            refs.append(upload.json()["storage_ref"])
        item = {"product_name": "فستان نسائي بأكمام طويلة", "quantity": 1, "estimated_price": 25, "image_url": refs[0], "image_urls": refs}
        created = await client.post("/api/shopping/international/orders", headers=headers[0], json={"items": [item]})
        assert created.status_code == 201, created.text
        order_id = created.json()["data"]["id"]
        async with SessionFactory() as session:
            stored = await session.get(MODEL_BY_TABLE["international_orders"], uuid.UUID(order_id))
            assert stored.extra_data["items"][0]["image_urls"] == refs
        for index, reference in enumerate(refs):
            file_id = reference.split(":")[1]
            for prefix, actor in (("/api/orders/international-shopping", headers[0]), ("/api/admin-shopping/international-orders", headers[1])):
                view = await client.get(f"{prefix}/{order_id}/attachments/{file_id}", headers=actor)
                assert view.status_code == 200, (index, view.text)
                assert view.headers["content-type"].startswith("image/")
                assert "no-store" in view.headers["cache-control"]
            denied = await client.get(f"/api/orders/international-shopping/{order_id}/attachments/{file_id}", headers=headers[2])
            assert denied.status_code == 404
        excessive = await client.post("/api/shopping/international/orders", headers=headers[0], json={"items": [{**item, "image_urls": refs * 4}]})
        assert excessive.status_code == 422, excessive.text
        foreign = await client.post("/api/shopping/international/orders", headers=headers[2], json={"items": [item]})
        assert foreign.status_code == 422, foreign.text
        files = [("file", (f"transfer-{index}.png", image.getvalue(), "image/png")) for index in range(3)]
        uploaded = await client.post(f"/api/finance/international-orders/{order_id}/receipt", headers=headers[1], files=files)
        assert uploaded.status_code == 201, uploaded.text
        receipt_refs = uploaded.json()["data"]["receipt_urls"]
        assert len(receipt_refs) == len(set(receipt_refs)) == 3
        payment = await client.post(f"/api/finance/international-orders/{order_id}/payments", headers=headers[1], json={"amount": 7000, "payment_method": "cash", "receipt_url": receipt_refs[0], "receipt_urls": receipt_refs, "status": "pending"})
        assert payment.status_code == 201, payment.text
        listing = await client.get(f"/api/finance/international-orders/{order_id}/payments", headers=headers[1])
        assert listing.json()["data"][0]["receipt_urls"] == receipt_refs
        async with SessionFactory() as session:
            model = MODEL_BY_TABLE["international_order_payments"]
            payments = list((await session.execute(select(model).where(model.order_id == uuid.UUID(order_id)))).scalars())
            assert len(payments) == 1 and float(payments[0].amount) == 7000
            assert payments[0].extra_data["receipt_urls"] == receipt_refs
        for ref in receipt_refs:
            signed = await client.post("/receipts/signed-url", headers=headers[1], json={"receiptPath": ref})
            assert signed.status_code == 200, signed.text
            denied = await client.post("/receipts/signed-url", headers=headers[2], json={"receiptPath": ref})
            assert denied.status_code in (403, 404)
        invalid = await client.post(f"/api/finance/international-orders/{order_id}/payments", headers=headers[1], json={"amount": 7000, "receipt_urls": ["https://example.com/receipt.png"]})
        assert invalid.status_code == 422, invalid.text
        excessive = await client.post(f"/api/finance/international-orders/{order_id}/receipt", headers=headers[1], files=files * 4)
        assert excessive.status_code == 422, excessive.text

