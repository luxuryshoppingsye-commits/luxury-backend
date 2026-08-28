from __future__ import annotations

import json
import uuid
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Order, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio

PREFIX = "PAYMENT_REFUND_SECURITY"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing payment/refund tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing payment/refund tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if not settings.database_is_test:
        pytest.fail("Refusing payment/refund tests outside a trusted test database", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing payment/refund tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing payment/refund tests on recovery database", pytrace=False)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(session, run_id: str, role: str) -> tuple[User, str]:
    password = "ValidPass123"
    email = f"{run_id}-{role}-{uuid.uuid4().hex[:8]}@example.com"
    user = User(email=email, password_hash=hash_password(password), is_active=True)
    session.add(user)
    await session.flush()
    session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"{PREFIX} {role}"))
    session.add(UserRole(user_id=user.id, role=role))
    return user, password


async def _login(client: AsyncClient, user: User, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": user.email, "password": password})
    assert response.status_code == 200, response.text
    return _headers(response.json()["access_token"])


async def _seed_order(run_id: str) -> dict[str, object]:
    async with SessionFactory() as session:
        customer, customer_password = await _seed_user(session, run_id, "customer")
        other_customer, other_password = await _seed_user(session, run_id, "customer")
        finance, finance_password = await _seed_user(session, run_id, "finance")
        logistics, logistics_password = await _seed_user(session, run_id, "logistics")
        order = Order(
            order_number=f"PRS-{uuid.uuid4().hex[:10].upper()}",
            user_id=customer.id,
            created_by=customer.id,
            status="pending",
            total=Decimal("100.00"),
            subtotal=Decimal("100.00"),
            discount_total=Decimal("0.00"),
            shipping_total=Decimal("0.00"),
            currency_code="YER",
            payment_method="wallet_transfer",
            payment_status="pending",
            shipping_address={"city": "Sanaa"},
            extra_data={"run_id": run_id},
        )
        session.add(order)
        await session.flush()
        payment_model = MODEL_BY_TABLE["order_payments"]
        session.add(payment_model(order_id=order.id, status="pending", type="wallet_transfer", amount=Decimal("100.00")))
        await session.commit()
        return {
            "order_id": order.id,
            "customer": (customer, customer_password),
            "other_customer": (other_customer, other_password),
            "finance": (finance, finance_password),
            "logistics": (logistics, logistics_password),
        }


async def test_receipt_upload_review_signed_url_and_refund_security_contract() -> None:
    _assert_safe_database()
    run_id = f"prs-{uuid.uuid4().hex[:10]}"
    seeded = await _seed_order(run_id)
    order_id = seeded["order_id"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        customer, customer_password = seeded["customer"]  # type: ignore[misc]
        other_customer, other_password = seeded["other_customer"]  # type: ignore[misc]
        finance, finance_password = seeded["finance"]  # type: ignore[misc]
        logistics, logistics_password = seeded["logistics"]  # type: ignore[misc]
        customer_headers = await _login(client, customer, customer_password)
        other_headers = await _login(client, other_customer, other_password)
        finance_headers = await _login(client, finance, finance_password)
        logistics_headers = await _login(client, logistics, logistics_password)

        no_file = await client.post(
            f"/orders/{order_id}/payment-receipt",
            headers=customer_headers,
            json={"amount": "100.00", "receipt_url": "https://example.invalid/receipt.png"},
        )
        assert no_file.status_code == 422
        assert no_file.json()["detail"] == "payment_proof_file_required"

        base64_payload = await client.post(
            f"/orders/{order_id}/payment-receipt",
            headers=customer_headers,
            json={"amount": "100.00", "dataBase64": "ZmlsZQ=="},
        )
        assert base64_payload.status_code == 422

        storage_receipt_json = await client.post(
            "/storage/upload",
            headers=customer_headers,
            json={"bucket": "receipts", "fileName": "receipt.png", "dataBase64": "ZmlsZQ=="},
        )
        assert storage_receipt_json.status_code == 422
        assert storage_receipt_json.json()["detail"] == "multipart_file_required"

        empty_file = await client.post(
            f"/orders/{order_id}/payment-receipt",
            headers=customer_headers,
            data={"amount": "100.00"},
            files={"file": ("empty.png", b"", "image/png")},
        )
        assert empty_file.status_code == 422

        receipt = await client.post(
            f"/orders/{order_id}/payment-receipt",
            headers=customer_headers,
            data={"amount": "100.00", "paymentMethod": "wallet_transfer", "transactionReference": "TX-OK"},
            files={"file": ("receipt.png", PNG_BYTES, "image/png")},
        )
        assert receipt.status_code == 201, receipt.text
        receipt_body = receipt.json()
        assert receipt_body["status"] == "pending_review"
        assert receipt_body["receipt_url"].startswith("receipt:")
        receipt_id = receipt_body["id"]

        async with SessionFactory() as session:
            receipt_model = MODEL_BY_TABLE["payment_receipts"]
            row = await session.get(receipt_model, uuid.UUID(receipt_id))
            assert row is not None
            assert row.image_url == f"receipt:{receipt_id}"
            serialized = json.dumps(row.extra_data, default=str)
            assert "dataBase64" not in serialized
            assert "base64" not in serialized.lower()
            assert "http://" not in serialized and "https://" not in serialized
            assert row.extra_data["storage_key"].startswith("_private/payment-receipts/")

        invalid_status = await client.post(
            f"/payments/{receipt_id}/review",
            headers=finance_headers,
            json={"status": "anything"},
        )
        assert invalid_status.status_code == 422

        logistics_review = await client.post(
            f"/payments/{receipt_id}/review",
            headers=logistics_headers,
            json={"status": "approved"},
        )
        assert logistics_review.status_code == 403

        direct_payment_receipt_url = await client.post(
            f"/api/payments/orders/{order_id}",
            headers=finance_headers,
            json={"amount": "5.00", "status": "pending", "receipt_url": "https://example.invalid/receipt.png"},
        )
        assert direct_payment_receipt_url.status_code == 422
        assert direct_payment_receipt_url.json()["detail"] == "payment_receipts_must_use_order_endpoint"

        direct_payment_random_status = await client.post(
            f"/api/payments/orders/{order_id}",
            headers=finance_headers,
            json={"amount": "5.00", "status": "anything"},
        )
        assert direct_payment_random_status.status_code == 422
        assert direct_payment_random_status.json()["detail"] == "invalid_payment_status"

        logistics_payment = await client.post(
            f"/api/payments/orders/{order_id}",
            headers=logistics_headers,
            json={"amount": "5.00", "status": "pending"},
        )
        assert logistics_payment.status_code == 403

        review = await client.post(
            f"/payments/{receipt_id}/review",
            headers=finance_headers,
            json={"status": "approved"},
        )
        assert review.status_code == 200, review.text
        assert review.json()["status"] == "approved"

        signed = await client.post(
            "/receipts/signed-url",
            headers=customer_headers,
            json={"receiptPath": f"receipt:{receipt_id}"},
        )
        assert signed.status_code == 200, signed.text
        signed_url = signed.json()["url"]
        assert "/receipts/access?token=" in signed_url
        assert "/uploads/" not in signed_url

        other_signed = await client.post(
            "/receipts/signed-url",
            headers=other_headers,
            json={"receiptPath": f"receipt:{receipt_id}"},
        )
        assert other_signed.status_code == 404

        access = await client.get(signed_url)
        assert access.status_code == 200
        assert access.content.startswith(b"RIFF") and access.content[8:12] == b"WEBP"
        assert access.headers["content-type"].startswith("image/webp")

        token = signed_url.split("token=", 1)[1]
        raw_part, sig_part = token.split(".", 1)
        tampered_sig = ("A" if sig_part[0] != "A" else "B") + sig_part[1:]
        tampered_token = f"{raw_part}.{tampered_sig}"
        tampered = await client.get(f"/receipts/access?token={tampered_token}")
        assert tampered.status_code == 403

        logistics_refund = await client.post(
            f"/orders/{order_id}/refund",
            headers=logistics_headers,
            json={"amount": "10.00", "reason": "not allowed"},
        )
        assert logistics_refund.status_code == 403

        refund = await client.post(
            f"/orders/{order_id}/refund",
            headers={**finance_headers, "Idempotency-Key": f"{run_id}-refund"},
            json={"amount": "10.00", "reason": "manual verification required"},
        )
        assert refund.status_code == 201, refund.text
        assert refund.json()["status"] == "requires_manual_action"
        assert refund.json()["requires_manual_action"] is True
        refund_id = refund.json()["id"]

        processing = await client.patch(
            f"/api/operations/refunds/{refund_id}/status",
            headers=finance_headers,
            json={"status": "manual_processing"},
        )
        assert processing.status_code == 200, processing.text
        assert processing.json()["data"]["status"] == "manual_processing"

        completed = await client.patch(
            f"/api/operations/refunds/{refund_id}/status",
            headers=finance_headers,
            json={
                "status": "manual_completed",
                "completionEvidence": f"{run_id}-refund-voucher",
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["data"]["status"] == "manual_completed"
        assert completed.json()["data"]["order_payment_status"] == "partially_refunded"

        async with SessionFactory() as session:
            order = await session.get(Order, order_id)
            assert order is not None
            assert order.payment_status == "partially_refunded"
            refunds_model = MODEL_BY_TABLE["refunds"]
            refund_rows = list(
                (
                    await session.execute(select(refunds_model).where(refunds_model.order_id == order_id))
                ).scalars()
            )
            assert len(refund_rows) == 1
            assert refund_rows[0].status == "manual_completed"
            assert refund_rows[0].extra_data["completion_evidence"] == f"{run_id}-refund-voucher"


async def test_external_receipt_url_records_are_not_signed() -> None:
    _assert_safe_database()
    run_id = f"prs-external-{uuid.uuid4().hex[:8]}"
    async with SessionFactory() as session:
        customer, customer_password = await _seed_user(session, run_id, "customer")
        finance, finance_password = await _seed_user(session, run_id, "finance")
        order = Order(
            order_number=f"PRS-EXT-{uuid.uuid4().hex[:8].upper()}",
            user_id=customer.id,
            created_by=customer.id,
            total=Decimal("20.00"),
            subtotal=Decimal("20.00"),
            payment_status="pending",
            shipping_address={},
        )
        session.add(order)
        await session.flush()
        receipt_model = MODEL_BY_TABLE["payment_receipts"]
        receipt = receipt_model(
            order_id=order.id,
            user_id=customer.id,
            status="pending_review",
            image_url="https://example.invalid/receipt.png",
            amount=Decimal("20.00"),
            extra_data={},
        )
        session.add(receipt)
        await session.commit()
        receipt_id = receipt.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        finance_headers = await _login(client, finance, finance_password)
        response = await client.post(
            "/receipts/signed-url",
            headers=finance_headers,
            json={"receiptPath": f"receipt:{receipt_id}"},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "external_receipt_url_rejected"
