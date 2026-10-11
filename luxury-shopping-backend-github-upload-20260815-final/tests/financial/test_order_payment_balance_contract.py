from decimal import Decimal
import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio
from datetime import datetime, timezone

from app.config import get_settings
from app.database import SessionFactory
from app.main import app
from app.models import MODEL_BY_TABLE
from app.models.domain import Order, User, UserRole
from app.security.passwords import hash_password
from app.services.financial_calculator import approved_payment_total, receipt_amount_for_order


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def order_record():
    settings = get_settings()
    assert settings.is_test_environment and "127.0.0.1" in settings.database_url
    admin_id, owner_id, order_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    admin_email = f"ghaya{admin_id.hex[:8]}@gmail.com"
    async with SessionFactory() as session:
        session.add_all([
            User(id=admin_id, email=admin_email, password_hash=hash_password("Admin1234"), is_active=True),
            User(id=owner_id, email=f"sara{owner_id.hex[:8]}@gmail.com", password_hash=hash_password("Admin1234"), is_active=True),
        ])
        await session.flush()
        session.add(UserRole(user_id=admin_id, role="admin"))
        session.add(Order(id=order_id, user_id=owner_id, order_number=f"LS-BLOUSE-{order_id.hex[:8]}",
                          total=2200, subtotal=2200, currency_code="YER", status="confirmed",
                          payment_status="paid", extra_data={"paid_amount": "2200.00", "remaining_balance": "0.00", "customer_note": "بلوزة نسائية"}))
        await session.flush()
        session.add(MODEL_BY_TABLE["order_payments"](order_id=order_id, amount=1000, status="confirmed", type="cash"))
        session.add(MODEL_BY_TABLE["order_payments"](order_id=order_id, amount=2200, status="pending", type="bank_transfer"))
        session.add(MODEL_BY_TABLE["payment_receipts"](order_id=order_id, user_id=owner_id, amount=2200, status="pending_review"))
        await session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8106") as client:
        response = await client.post("/api/auth/login", json={"email": admin_email, "password": "Admin1234"})
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = "Bearer " + response.json()["session"]["access_token"]
        yield order_id, client


async def balances(order_id):
    async with SessionFactory() as session:
        order = await session.get(Order, order_id)
        return order.extra_data, order.payment_status, await approved_payment_total(session, order_id)


@pytest.mark.parametrize("status, counted", [("confirmed", True), ("approved", True), ("paid", True),
                                           ("pending", False), ("rejected", False), ("failed", False), ("completed", False)])
async def test_only_confirmed_order_payments_are_counted(order_record, status, counted):
    order_id, _ = order_record
    async with SessionFactory() as session:
        session.add(MODEL_BY_TABLE["order_payments"](order_id=order_id, amount=100, status=status, type="cash"))
        await session.flush()
        assert await approved_payment_total(session, order_id) == Decimal("1100" if counted else "1000")


@pytest.mark.parametrize("amount", ["10", "1000", "1200"])
async def test_pending_receipts_and_obsolete_saved_balance_do_not_block_upload(order_record, amount):
    order_id, _ = order_record
    async with SessionFactory() as session:
        order = await session.get(Order, order_id)
        assert await receipt_amount_for_order(session, order, amount) == Decimal(amount)


async def test_conflict_contains_real_balance_values(order_record):
    order_id, client = order_record
    response = await client.post(f"/api/orders/{order_id}/payment-receipt",
                                 data={"amount": "1201", "paymentMethod": "bank_transfer"},
                                 files={"file": ("سند-بلوزة.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "payment_exceeds_order_balance"
    detail = response.json()["detail"]
    assert {key: detail[key] for key in ("order_total", "current_paid", "remaining_balance", "requested_amount")} == {
        "order_total": 2200, "current_paid": 1000, "remaining_balance": 1200, "requested_amount": 1201,
    }


@pytest.mark.parametrize("method", ["bank_transfer", "wallet_transfer"])
async def test_upload_then_create_edit_delete_payment_resynchronizes_balance(order_record, method):
    order_id, client = order_record
    # A valid one-page PDF with a transfer reference, kept in memory.
    import io
    from reportlab.pdfgen.canvas import Canvas
    stream = io.BytesIO()
    canvas = Canvas(stream)
    canvas.drawString(72, 740, "Bank transfer receipt - Amount: 1000 YER")
    canvas.save()
    response = await client.post(f"/api/orders/{order_id}/payment-receipt",
                                 data={"amount": "1000", "paymentMethod": method},
                                 files={"file": ("سند-بلوزة.pdf", stream.getvalue(), "application/pdf")})
    assert response.status_code == 201, response.text
    receipt_ref = response.json()["receipt_url"]
    assert receipt_ref.startswith("receipt:")
    payment = await client.post(f"/api/payments/orders/{order_id}", json={
        "amount": 1000, "payment_method": method, "receipt_url": receipt_ref, "status": "confirmed",
    })
    assert payment.status_code == 201, payment.text
    payment_id = payment.json()["data"]["id"]
    extra, status, paid = await balances(order_id)
    assert (paid, extra["paid_amount"], extra["remaining_balance"], status) == (Decimal("2000"), "2000.00", "200.00", "partial")
    assert extra["customer_note"] == "بلوزة نسائية"
    duplicate = await client.post(f"/api/payments/orders/{order_id}", json={
        "amount": 100, "payment_method": method, "receipt_url": receipt_ref, "status": "confirmed",
    })
    assert duplicate.status_code == 409, duplicate.text
    response = await client.patch(f"/api/payments/orders/records/{payment_id}", json={"status": "pending"})
    assert response.status_code == 200, response.text
    assert (await balances(order_id))[0]["remaining_balance"] == "1200.00"
    response = await client.patch(f"/api/payments/orders/records/{payment_id}", json={"status": "confirmed", "amount": 800})
    assert response.status_code == 200, response.text
    assert (await balances(order_id))[0]["remaining_balance"] == "400.00"
    response = await client.delete(f"/api/payments/orders/records/{payment_id}")
    assert response.status_code == 200, response.text
    extra, status, paid = await balances(order_id)
    assert (paid, extra["paid_amount"], extra["remaining_balance"], status) == (Decimal("1000"), "1000.00", "1200.00", "partial")


async def test_legacy_receipt_and_manual_payment_are_not_counted_twice(order_record):
    order_id, _ = order_record
    receipt_id = uuid.uuid4()
    async with SessionFactory() as session:
        session.add(MODEL_BY_TABLE["order_payments"](order_id=order_id, amount=100, status="confirmed", type="bank_transfer",
                                                    extra_data={"receipt_url": f"receipt:{receipt_id}"}))
        session.add(MODEL_BY_TABLE["payments"](order_id=order_id, amount=100, status="approved", type="payment_receipt",
                                              extra_data={"payment_receipt_id": str(receipt_id)}))
        session.add(MODEL_BY_TABLE["payments"](order_id=order_id, amount=50, status="approved", type="payment_receipt",
                                              extra_data={"payment_receipt_id": str(uuid.uuid4())}))
        await session.flush()
        assert await approved_payment_total(session, order_id) == Decimal("1150")


async def test_confirmed_creation_cannot_exceed_remaining_balance(order_record):
    order_id, client = order_record
    response = await client.post(f"/api/payments/orders/{order_id}", json={"amount": 1201, "payment_method": "cash", "status": "confirmed"})
    assert response.status_code == 409, response.text
    assert (await balances(order_id))[2] == Decimal("1000")


async def test_soft_deleted_payment_does_not_reduce_remaining_balance(order_record):
    order_id, _ = order_record
    async with SessionFactory() as session:
        session.add(MODEL_BY_TABLE["order_payments"](order_id=order_id, amount=1200, status="confirmed", type="cash", deleted_at=datetime.now(timezone.utc)))
        await session.flush()
        assert await approved_payment_total(session, order_id) == Decimal("1000")


async def test_concurrent_confirmations_cannot_overpay_order(order_record):
    order_id, client = order_record
    responses = await asyncio.gather(*[
        client.post(f"/api/payments/orders/{order_id}", json={"amount": 800, "payment_method": "cash", "status": "confirmed"})
        for _ in range(2)
    ])
    assert sorted(response.status_code for response in responses) == [201, 409]
    extra, _, paid = await balances(order_id)
    assert paid == Decimal("1800") and extra["remaining_balance"] == "400.00"


async def test_review_of_an_excess_pending_receipt_is_rolled_back(order_record):
    order_id, client = order_record
    from sqlalchemy import select
    async with SessionFactory() as session:
        model = MODEL_BY_TABLE["payment_receipts"]
        receipt = (await session.execute(select(model).where(model.order_id == order_id))).scalar_one()
        receipt_id = receipt.id
    response = await client.post(f"/payments/{receipt_id}/review", json={"status": "approved"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["remaining_balance"] == 1200
    assert (await balances(order_id))[2] == Decimal("1000")
    async with SessionFactory() as session:
        receipt = await session.get(MODEL_BY_TABLE["payment_receipts"], receipt_id)
        assert receipt.status == "pending_review"


async def test_order_details_ignore_obsolete_saved_paid_amount(order_record):
    order_id, client = order_record
    response = await client.get(f"/api/orders/{order_id}")
    assert response.status_code == 200, response.text
    order = response.json()["order"]
    assert (order["paid_amount"], order["remaining_balance"], order["payment_status"]) == ("1000.00", "1200.00", "partial")


async def test_receipt_review_and_manual_confirmation_count_the_same_receipt_once(order_record):
    order_id, client = order_record
    import io
    from reportlab.pdfgen.canvas import Canvas
    stream = io.BytesIO()
    document = Canvas(stream)
    document.drawString(72, 740, "Wallet transfer receipt - Amount: 300 YER")
    document.save()
    response = await client.post(f"/api/orders/{order_id}/payment-receipt", data={"amount": "300"},
                                 files={"file": ("سند-محفظة.pdf", stream.getvalue(), "application/pdf")})
    assert response.status_code == 201, response.text
    receipt = response.json()
    payment = await client.post(f"/api/payments/orders/{order_id}", json={
        "amount": 300, "payment_method": "wallet_transfer", "receipt_url": receipt["receipt_url"], "status": "pending",
    })
    assert payment.status_code == 201, payment.text
    response = await client.post(f"/payments/{receipt['id']}/review", json={"status": "approved"})
    assert response.status_code == 200, response.text
    assert (await balances(order_id))[2] == Decimal("1300")
    response = await client.patch(f"/api/payments/orders/records/{payment.json()['data']['id']}", json={"status": "confirmed"})
    assert response.status_code == 200, response.text
    extra, _, paid = await balances(order_id)
    assert paid == Decimal("1300") and extra["remaining_balance"] == "900.00"


async def test_customer_counts_use_live_ledgers_instead_of_obsolete_paid_cache(order_record):
    from sqlalchemy import select
    order_id, admin = order_record
    async with SessionFactory() as session:
        order = await session.get(Order, order_id)
        owner = await session.get(User, order.user_id)
        email = owner.email
        model = MODEL_BY_TABLE["order_payments"]
        confirmed = (await session.execute(select(model).where(model.order_id == order_id, model.status == "confirmed"))).scalar_one()
        payment_id = confirmed.id
    response = await admin.delete(f"/api/payments/orders/records/{payment_id}")
    assert response.status_code == 200, response.text
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8106") as customer:
        login = await customer.post("/api/auth/login", json={"email": email, "password": "Admin1234"})
        assert login.status_code == 200, login.text
        customer.headers["Authorization"] = "Bearer " + login.json()["session"]["access_token"]
        response = await customer.get("/api/orders/counts")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["unpaid"] == 1


async def test_store_balance_guards_do_not_change_local_request_payments(order_record):
    from app.api.routes.operations import _api_create
    order_id, _ = order_record
    local_id, receipt_id = uuid.uuid4(), uuid.uuid4()
    async with SessionFactory() as session:
        for _ in range(2):
            await _api_create(session, "order_payments", {
                "order_id": local_id, "local_request_id": str(local_id), "amount": 100,
                "status": "confirmed", "type": "wallet_transfer", "receipt_url": f"receipt:{receipt_id}",
            })
        await session.commit()
        assert await approved_payment_total(session, order_id) == Decimal("1000")
