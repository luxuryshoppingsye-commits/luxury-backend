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
from backend.app.models.domain import Order, OrderItem, Product, Profile, User, UserRole
from backend.app.security.passwords import hash_password
from backend.app.services.merchant_order_scope import merchant_payload_forbidden_keys


pytestmark = pytest.mark.asyncio


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing multi-merchant isolation tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing multi-merchant isolation tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if not settings.database_is_test:
        pytest.fail("Refusing multi-merchant isolation tests outside a trusted test database", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing multi-merchant isolation tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing multi-merchant isolation tests on recovery database", pytrace=False)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(session, email: str, roles: set[str], full_name: str) -> tuple[User, str]:
    password = "ValidPass123"
    user = User(email=email, password_hash=hash_password(password), is_active=True)
    session.add(user)
    await session.flush()
    session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=full_name))
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role))
    return user, password


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return _headers(response.json()["access_token"])


async def _seed_mixed_order(run_id: str) -> dict[str, object]:
    async with SessionFactory() as session:
        customer, customer_password = await _seed_user(
            session,
            f"{run_id}-customer@example.com",
            {"customer"},
            "Isolation Customer",
        )
        admin, admin_password = await _seed_user(
            session,
            f"{run_id}-admin@example.com",
            {"admin"},
            "Isolation Admin",
        )
        merchant_a, merchant_a_password = await _seed_user(
            session,
            f"{run_id}-merchant-a@example.com",
            {"partner"},
            "Merchant A",
        )
        merchant_b, merchant_b_password = await _seed_user(
            session,
            f"{run_id}-merchant-b@example.com",
            {"partner"},
            "Merchant B",
        )
        merchant_c, merchant_c_password = await _seed_user(
            session,
            f"{run_id}-merchant-c@example.com",
            {"partner"},
            "Merchant C",
        )
        product_a = Product(
            name=f"{run_id} Merchant A Product",
            sku=f"{run_id}-A".upper(),
            price=Decimal("10000.00"),
            stock_quantity=20,
            is_active=True,
            approval_status="approved",
            partner_id=merchant_a.id,
        )
        product_b = Product(
            name=f"{run_id} Merchant B Product",
            sku=f"{run_id}-B".upper(),
            price=Decimal("20000.00"),
            stock_quantity=20,
            is_active=True,
            approval_status="approved",
            partner_id=merchant_b.id,
        )
        session.add_all([product_a, product_b])
        await session.flush()
        order = Order(
            order_number=f"MIX-{run_id}",
            user_id=customer.id,
            created_by=customer.id,
            status="processing",
            subtotal=Decimal("45000.00"),
            discount_total=Decimal("5000.00"),
            shipping_total=Decimal("1500.00"),
            total=Decimal("41500.00"),
            currency_code="YER",
            payment_method="bank_transfer",
            payment_status="paid",
            shipping_address={
                "recipient_name": "Sensitive Customer",
                "phone": "+967777000111",
                "city": "Sanaa",
                "address": "Sensitive full address",
            },
            notes=f"{run_id} customer private note for all merchants",
            extra_data={"run_id": run_id, "other_partner_id": str(merchant_b.id)},
        )
        session.add(order)
        await session.flush()
        item_a = OrderItem(
            order_id=order.id,
            product_id=product_a.id,
            product_name=product_a.name,
            quantity=2,
            unit_price=Decimal("12500.00"),
            total_price=Decimal("25000.00"),
            partner_id=merchant_a.id,
            extra_data={"run_id": run_id, "private": "merchant-a-only"},
        )
        item_b = OrderItem(
            order_id=order.id,
            product_id=product_b.id,
            product_name=product_b.name,
            quantity=1,
            unit_price=Decimal("20000.00"),
            total_price=Decimal("20000.00"),
            partner_id=merchant_b.id,
            extra_data={"run_id": run_id, "private": "merchant-b-only"},
        )
        session.add_all([item_a, item_b])
        payment_model = MODEL_BY_TABLE["order_payments"]
        receipt_model = MODEL_BY_TABLE["payment_receipts"]
        history_model = MODEL_BY_TABLE["order_status_history"]
        shipping_model = MODEL_BY_TABLE["order_shipping"]
        shipping_history_model = MODEL_BY_TABLE["shipping_history"]
        session.add(payment_model(order_id=order.id, status="paid", type="bank_transfer", amount=Decimal("41500.00"), extra_data={"transaction_reference": f"TX-{run_id}"}))
        session.add(receipt_model(order_id=order.id, user_id=customer.id, status="approved", image_url=f"/uploads/receipts/{run_id}.png", amount=Decimal("41500.00")))
        session.add(history_model(order_id=order.id, status="processing", notes=f"{run_id} global note mentions {product_b.name}"))
        session.add(shipping_model(order_id=order.id, status="assigned", fee=Decimal("1500.00"), description=f"{run_id} full shipping operation"))
        session.add(shipping_history_model(order_id=order.id, status="assigned", notes=f"{run_id} courier internal note"))
        await session.commit()
        return {
            "run_id": run_id,
            "order_id": order.id,
            "order_number": order.order_number,
            "customer": (customer.email, customer_password, customer.id),
            "admin": (admin.email, admin_password),
            "merchant_a": (merchant_a.email, merchant_a_password, merchant_a.id),
            "merchant_b": (merchant_b.email, merchant_b_password, merchant_b.id),
            "merchant_c": (merchant_c.email, merchant_c_password, merchant_c.id),
            "product_a_name": product_a.name,
            "product_b_name": product_b.name,
            "product_a_id": product_a.id,
            "product_b_id": product_b.id,
        }


def _payload_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _assert_no_cross_merchant_or_customer_leak(payload: object, seeded: dict[str, object]) -> None:
    text = _payload_text(payload)
    _, _, customer_id = seeded["customer"]  # type: ignore[misc]
    forbidden_fragments = [
        seeded["product_b_name"],
        str(seeded["product_b_id"]),
        str(seeded["merchant_b"][2]),  # type: ignore[index]
        str(customer_id),
        "Sensitive Customer",
        "+967777000111",
        "Sensitive full address",
        "bank_transfer",
        "TX-",
        "receipt",
        "global note",
        "courier internal",
        "customer private note",
    ]
    for fragment in forbidden_fragments:
        assert str(fragment) not in text
    assert not merchant_payload_forbidden_keys(payload)


async def test_partner_order_list_detail_report_and_generic_resources_are_scoped() -> None:
    _assert_safe_database()
    run_id = f"mmiso-{uuid.uuid4().hex[:10]}"
    seeded = await _seed_mixed_order(run_id)
    order_id = seeded["order_id"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        merchant_a_email, merchant_a_password, merchant_a_id = seeded["merchant_a"]  # type: ignore[misc]
        merchant_b_email, merchant_b_password, merchant_b_id = seeded["merchant_b"]  # type: ignore[misc]
        merchant_c_email, merchant_c_password, _ = seeded["merchant_c"]  # type: ignore[misc]
        admin_email, admin_password = seeded["admin"]  # type: ignore[misc]
        merchant_a_headers = await _login(client, merchant_a_email, merchant_a_password)
        merchant_b_headers = await _login(client, merchant_b_email, merchant_b_password)
        merchant_c_headers = await _login(client, merchant_c_email, merchant_c_password)
        admin_headers = await _login(client, admin_email, admin_password)

        list_a = await client.get("/orders", headers=merchant_a_headers, params={"scope": "partner", "limit": 10})
        assert list_a.status_code == 200, list_a.text
        rows_a = [row for row in list_a.json() if row["order_id"] == str(order_id)]
        assert len(rows_a) == 1
        assert rows_a[0]["merchant_total"] == "25000.00"
        assert {item["product_name"] for item in rows_a[0]["items"]} == {seeded["product_a_name"]}
        assert rows_a[0]["partner_id"] == str(merchant_a_id)
        _assert_no_cross_merchant_or_customer_leak(rows_a[0], seeded)

        list_b = await client.get("/api/partner/orders", headers=merchant_b_headers)
        assert list_b.status_code == 200, list_b.text
        rows_b = [row for row in list_b.json()["data"] if row["order_id"] == str(order_id)]
        assert len(rows_b) == 1
        assert rows_b[0]["merchant_total"] == "20000.00"
        assert {item["product_name"] for item in rows_b[0]["items"]} == {seeded["product_b_name"]}
        assert rows_b[0]["partner_id"] == str(merchant_b_id)

        detail_a = await client.get(f"/orders/{order_id}", headers=merchant_a_headers, params={"scope": "partner"})
        assert detail_a.status_code == 200, detail_a.text
        detail_payload = detail_a.json()
        assert detail_payload["order"]["merchant_total"] == "25000.00"
        assert {item["product_name"] for item in detail_payload["items"]} == {seeded["product_a_name"]}
        assert "payments" not in detail_payload
        assert "shipping" not in detail_payload
        assert "notes" not in detail_payload
        _assert_no_cross_merchant_or_customer_leak(detail_payload, seeded)

        forbidden_detail = await client.get(f"/orders/{order_id}", headers=merchant_c_headers, params={"scope": "partner"})
        assert forbidden_detail.status_code == 404

        report_a = await client.get("/partner/reports/summary", headers=merchant_a_headers)
        assert report_a.status_code == 200, report_a.text
        report_body = report_a.json()
        assert report_body["ordersCount"] == 1
        assert report_body["revenue"] == "25000.00"
        assert report_body["period"] == "month"
        assert report_body["grossRevenue"] == "25000.00"
        assert report_body["averageOrder"] == "25000.00"
        assert report_body["salesSeries"]
        assert any(row["revenue"] == "25000.00" for row in report_body["salesSeries"])
        assert report_body["statusBreakdown"] == [
            {"status": "processing", "orders": 1, "revenue": "25000.00"}
        ]
        assert report_body["topProducts"] == [
            {
                "name": seeded["product_a_name"],
                "quantity": 2,
                "revenue": "25000.00",
            }
        ]
        assert report_body["customerSummary"] == {
            "uniqueCustomers": 1,
            "returningCustomers": 0,
            "oneTimeCustomers": 1,
            "averageOrdersPerCustomer": "1.00",
        }
        assert report_body["aggregation"] == "own_order_items_successful_payments_minus_refunds"
        _assert_no_cross_merchant_or_customer_leak(report_body, seeded)

        for table in ("orders", "order_items", "order_payments", "payment_receipts", "order_status_history", "order_shipping"):
            response = await client.post(
                f"/resources/{table}/query",
                headers=merchant_a_headers,
                json={"operation": "select", "limit": 1},
            )
            assert response.status_code == 403, f"{table}: {response.text}"
            assert response.json()["detail"] == "merchant_typed_endpoint_required"

        admin_detail = await client.get(f"/orders/{order_id}", headers=admin_headers)
        assert admin_detail.status_code == 200, admin_detail.text
        admin_payload = admin_detail.json()
        assert len(admin_payload["items"]) == 2
        assert admin_payload["order"]["total"] == "41500.00"
        assert admin_payload["payments"]
        assert seeded["product_b_name"] in _payload_text(admin_payload)

    async with SessionFactory() as session:
        duplicate_pairs = (
            await session.execute(
                select(OrderItem.partner_id, OrderItem.order_id)
                .where(OrderItem.order_id == order_id)
                .group_by(OrderItem.partner_id, OrderItem.order_id)
            )
        ).all()
        assert len(duplicate_pairs) == 2
