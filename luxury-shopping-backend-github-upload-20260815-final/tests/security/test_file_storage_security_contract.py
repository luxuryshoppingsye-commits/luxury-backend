from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import FileAsset, Order, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)
INFECTED_PNG = PNG_BYTES + b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!"


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing file storage tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing file storage tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if not settings.database_is_test:
        pytest.fail("Refusing file storage tests outside a trusted test database", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing file storage tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing file storage tests on recovery database", pytrace=False)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(session, run_id: str, role: str) -> tuple[User, str]:
    password = "ValidPass123"
    email = f"{run_id}-{role}-{uuid.uuid4().hex[:8]}@example.com"
    user = User(email=email, password_hash=hash_password(password), is_active=True)
    session.add(user)
    await session.flush()
    session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"{run_id} {role}"))
    session.add(UserRole(user_id=user.id, role=role))
    return user, password


async def _login(client: AsyncClient, user: User, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": user.email, "password": password})
    assert response.status_code == 200, response.text
    return _headers(response.json()["access_token"])


async def test_secure_file_storage_contract() -> None:
    _assert_safe_database()
    run_id = f"file-storage-{uuid.uuid4().hex[:8]}"
    async with SessionFactory() as session:
        admin, admin_password = await _seed_user(session, run_id, "admin")
        customer, customer_password = await _seed_user(session, run_id, "customer")
        order = Order(
            order_number=f"FS-{uuid.uuid4().hex[:8].upper()}",
            user_id=customer.id,
            created_by=customer.id,
            status="pending",
            total=Decimal("25.00"),
            subtotal=Decimal("25.00"),
            payment_method="wallet_transfer",
            payment_status="pending",
            shipping_address={"city": "Sanaa"},
            extra_data={"run_id": run_id},
        )
        session.add(order)
        await session.flush()
        order_id = order.id
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_headers = await _login(client, admin, admin_password)
        customer_headers = await _login(client, customer, customer_password)

        bucket_json = await client.post(
            "/storage/upload",
            headers=admin_headers,
            json={"bucket": "site-assets", "fileName": "asset.png", "dataBase64": "ZmFrZQ=="},
        )
        assert bucket_json.status_code == 422
        assert bucket_json.json()["detail"] == "multipart_file_required"

        raw_bucket = await client.post(
            "/storage/upload",
            headers=admin_headers,
            data={"bucket": "site-assets"},
            files={"file": ("asset.png", PNG_BYTES, "image/png")},
        )
        assert raw_bucket.status_code == 422
        assert raw_bucket.json()["detail"] == "client_storage_fields_forbidden"

        denied_customer_asset = await client.post(
            "/storage/upload",
            headers=customer_headers,
            data={"purpose": "site_asset"},
            files={"file": ("asset.png", PNG_BYTES, "image/png")},
        )
        assert denied_customer_asset.status_code == 403

        product_upload = await client.post(
            "/manage/product-image",
            headers=admin_headers,
            files={"file": ("product.png", PNG_BYTES, "image/png")},
        )
        assert product_upload.status_code == 201, product_upload.text
        product_body = product_upload.json()
        assert product_body["url"].startswith("http://testserver/uploads/products/")
        file_id = product_body["file_id"]

        async with SessionFactory() as session:
            asset = await session.get(FileAsset, uuid.UUID(file_id))
            assert asset is not None
            assert asset.policy_key == "product_image"
            assert asset.storage_key.startswith("products/")
            assert asset.scan_status == "clean"
            assert Path(get_settings().resolved_upload_dir / asset.storage_key).is_file()

        raw_delete = await client.post(
            "/storage/remove",
            headers=admin_headers,
            json={"path": product_body["url"]},
        )
        assert raw_delete.status_code == 422
        assert raw_delete.json()["detail"] == "raw_path_delete_forbidden"

        delete_ok = await client.post(
            "/storage/remove",
            headers=admin_headers,
            json={"fileId": file_id},
        )
        assert delete_ok.status_code == 200
        assert delete_ok.json()["removed"] == 1

        async with SessionFactory() as session:
            deleted = await session.get(FileAsset, uuid.UUID(file_id))
            assert deleted is not None
            assert deleted.deleted_at is not None
            assert deleted.status == "deleted"

        infected = await client.post(
            "/manage/product-image",
            headers=admin_headers,
            files={"file": ("infected.png", INFECTED_PNG, "image/png")},
        )
        assert infected.status_code == 422
        assert infected.json()["detail"]["code"] == "malware_or_active_content_detected"
        quarantined = list((get_settings().resolved_upload_dir / "_quarantine" / "products").glob("*.upload"))
        assert quarantined

        generic_receipt = await client.post(
            "/storage/upload",
            headers=customer_headers,
            data={"purpose": "payment_receipt"},
            files={"file": ("receipt.png", PNG_BYTES, "image/png")},
        )
        assert generic_receipt.status_code == 422
        assert generic_receipt.json()["detail"] == "payment_receipts_must_use_order_endpoint"

        receipt = await client.post(
            f"/orders/{order_id}/payment-receipt",
            headers=customer_headers,
            data={"amount": "25.00"},
            files={"file": ("receipt.png", PNG_BYTES, "image/png")},
        )
        assert receipt.status_code == 201, receipt.text
        receipt_id = receipt.json()["id"]

        async with SessionFactory() as session:
            receipt_model = MODEL_BY_TABLE["payment_receipts"]
            row = await session.get(receipt_model, uuid.UUID(receipt_id))
            assert row is not None
            assert row.extra_data["storage_key"].startswith("_private/payment-receipts/")
            assert row.extra_data["file_asset_id"]
            private_storage_key = row.extra_data["storage_key"]

        public_private_read = await client.get(f"/uploads/{private_storage_key}")
        assert public_private_read.status_code == 404
