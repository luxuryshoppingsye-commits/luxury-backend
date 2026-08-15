from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import (
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Profile,
    User,
    UserRole,
)
from backend.scripts.financial_reconciliation import reconcile_financial_integrity
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio


PREFIX = "CODEX_FINANCIAL_TEST"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
    b"\x02\xfeA\xe2!\xbc\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _decimal(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


async def _seed_user(email_suffix: str, role: str) -> tuple[str, str, uuid.UUID]:
    password = "ValidPass123"
    email = f"{PREFIX.lower()}_{email_suffix}_{uuid.uuid4().hex[:10]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"{PREFIX}_{role}"))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return email, password, user.id


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return _headers(response.json()["access_token"])


async def _seed_financial_catalog(customer_id: uuid.UUID, suffix: str) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID]:
    async with SessionFactory() as session:
        product = Product(
            name=f"{PREFIX}_Product_{suffix}",
            sku=f"CODEX-FIN-{suffix}",
            price=Decimal("1200.10"),
            stock_quantity=50,
            track_inventory=True,
            is_active=True,
            approval_status="approved",
        )
        session.add(product)
        await session.flush()

        variant = ProductVariant(
            product_id=product.id,
            sku=f"CODEX-FIN-VAR-{suffix}",
            size="M",
            color="Gold",
            price=Decimal("1000.55"),
            stock_quantity=10,
            is_active=True,
        )
        session.add(variant)

        coupon_model = MODEL_BY_TABLE["coupons"]
        coupon = coupon_model(
            code=f"FIN{suffix.upper()}",
            title=f"{PREFIX} Coupon",
            status="active",
            amount=Decimal("100.15"),
            is_active=True,
            extra_data={"usage_limit": 5, "per_user_limit": 1, "discount_type": "fixed"},
        )
        session.add(coupon)

        loyalty_model = MODEL_BY_TABLE["user_loyalty"]
        session.add(loyalty_model(user_id=customer_id, status="active", balance=Decimal("200.00")))

        shipping_model = MODEL_BY_TABLE["shipping_zones"]
        shipping = shipping_model(
            name=f"{PREFIX}_Sanaa_{suffix}",
            status="active",
            fee=Decimal("25.35"),
            is_active=True,
            sort_order=1,
        )
        session.add(shipping)

        await session.commit()
        return product.id, variant.id, coupon.code, shipping.id


async def test_checkout_payment_refund_financial_contract_uses_postgresql_values() -> None:
    suffix = uuid.uuid4().hex[:10]
    customer_email, customer_password, customer_id = await _seed_user(f"customer_{suffix}", "customer")
    admin_email, admin_password, _ = await _seed_user(f"admin_{suffix}", "admin")
    product_id, variant_id, coupon_code, shipping_zone_id = await _seed_financial_catalog(customer_id, suffix)

    expected_subtotal = Decimal("2001.10")
    expected_coupon = Decimal("100.15")
    expected_loyalty = Decimal("50.20")
    expected_discount = Decimal("150.35")
    expected_shipping = Decimal("25.35")
    expected_total = Decimal("1876.10")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        customer_headers = await _login(client, customer_email, customer_password)
        admin_headers = await _login(client, admin_email, admin_password)

        cart = await client.post(
            "/cart",
            headers=customer_headers,
            json={"productId": str(product_id), "variantId": str(variant_id), "quantity": 2},
        )
        assert cart.status_code == 201, cart.text

        checkout = await client.post(
            "/orders/checkout",
            headers={**customer_headers, "Idempotency-Key": f"{PREFIX}_checkout_{suffix}"},
            json={
                "paymentMethod": "wallet_transfer",
                "shippingZoneId": str(shipping_zone_id),
                "shippingCost": "9999.99",
                "couponCode": coupon_code,
                "couponDiscount": "999.99",
                "loyaltyPointsToRedeem": "50.20",
                "loyaltyDiscount": "888.88",
                "subtotal": "1.00",
                "total": "1.00",
                "shippingAddress": {
                    "recipientName": "Financial Customer",
                    "phone": "+967711222333",
                    "governorate": "Amanat Al Asimah",
                    "city": "Sanaa",
                    "address": f"{PREFIX} Street",
                    "shippingZoneId": str(shipping_zone_id),
                },
            },
        )
        assert checkout.status_code == 201, checkout.text
        order_payload = checkout.json()
        order_id = uuid.UUID(order_payload["id"])

        assert _decimal(order_payload["subtotal"]) == expected_subtotal
        assert _decimal(order_payload["discount_total"]) == expected_discount
        assert _decimal(order_payload["shipping_total"]) == expected_shipping
        assert _decimal(order_payload["total"]) == expected_total

        repeated = await client.post(
            "/orders/checkout",
            headers={**customer_headers, "Idempotency-Key": f"{PREFIX}_checkout_{suffix}"},
            json={
                "paymentMethod": "wallet_transfer",
                "shippingZoneId": str(shipping_zone_id),
                "shippingCost": "9999.99",
                "couponCode": coupon_code,
                "couponDiscount": "999.99",
                "loyaltyPointsToRedeem": "50.20",
                "loyaltyDiscount": "888.88",
                "subtotal": "1.00",
                "total": "1.00",
                "shippingAddress": {
                    "recipientName": "Financial Customer",
                    "phone": "+967711222333",
                    "governorate": "Amanat Al Asimah",
                    "city": "Sanaa",
                    "address": f"{PREFIX} Street",
                    "shippingZoneId": str(shipping_zone_id),
                },
            },
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["id"] == str(order_id)
        assert repeated.json()["idempotency_replayed"] is True

        receipt = await client.post(
            f"/orders/{order_id}/payment-receipt",
            headers=customer_headers,
            data={"amount": str(expected_total), "paymentMethod": "wallet_transfer"},
            files={"file": (f"{PREFIX}_receipt.png", PNG_BYTES, "image/png")},
        )
        assert receipt.status_code == 201, receipt.text
        assert _decimal(receipt.json()["amount"]) == expected_total
        assert receipt.json()["receipt_url"].startswith("receipt:")

        review = await client.post(
            f"/payments/{receipt.json()['id']}/review",
            headers=admin_headers,
            json={"status": "approved"},
        )
        assert review.status_code == 200, review.text

        refund = await client.post(
            f"/orders/{order_id}/refund",
            headers={**admin_headers, "Idempotency-Key": f"{PREFIX}_refund_{suffix}"},
            json={"amount": "300.10", "reason": f"{PREFIX} partial refund"},
        )
        assert refund.status_code == 201, refund.text
        refund_body = refund.json()
        assert _decimal(refund_body["amount"]) == Decimal("300.10")
        assert refund_body["status"] == "requires_manual_action"
        assert refund_body["requires_manual_action"] is True

        replay_refund = await client.post(
            f"/orders/{order_id}/refund",
            headers={**admin_headers, "Idempotency-Key": f"{PREFIX}_refund_{suffix}"},
            json={"amount": "300.10", "reason": f"{PREFIX} partial refund"},
        )
        assert replay_refund.status_code == 200, replay_refund.text
        assert replay_refund.json()["id"] == refund_body["id"]
        assert replay_refund.json()["idempotency_replayed"] is True

        conflicting_refund = await client.post(
            f"/orders/{order_id}/refund",
            headers={**admin_headers, "Idempotency-Key": f"{PREFIX}_refund_{suffix}"},
            json={"amount": "301.10", "reason": f"{PREFIX} changed refund"},
        )
        assert conflicting_refund.status_code == 409, conflicting_refund.text

        over_refund = await client.post(
            f"/orders/{order_id}/refund",
            headers={**admin_headers, "Idempotency-Key": f"{PREFIX}_refund_over_{suffix}"},
            json={"amount": "99999.99", "reason": f"{PREFIX} over refund"},
        )
        assert over_refund.status_code == 409, over_refund.text
        assert over_refund.json()["detail"] == "refund_exceeds_paid_amount"

        detail_before_price_change = await client.get(f"/orders/{order_id}", headers=customer_headers)
        assert detail_before_price_change.status_code == 200

        async with SessionFactory() as session:
            product = await session.get(Product, product_id)
            variant = await session.get(ProductVariant, variant_id)
            assert product is not None and variant is not None
            product.price = Decimal("9999.00")
            variant.price = Decimal("8888.00")
            await session.commit()

        detail_after_price_change = await client.get(f"/orders/{order_id}", headers=customer_headers)
        assert detail_after_price_change.status_code == 200
        item_payload = detail_after_price_change.json()["items"][0]
        assert _decimal(item_payload["unit_price"]) == Decimal("1000.55")
        assert _decimal(item_payload["total_price"]) == expected_subtotal

    async with SessionFactory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.subtotal == expected_subtotal
        assert order.discount_total == expected_discount
        assert order.shipping_total == expected_shipping
        assert order.total == expected_total
        assert order.payment_status == "paid"
        breakdown = order.extra_data["financial_breakdown"]
        assert breakdown["coupon_discount"] == str(expected_coupon)
        assert breakdown["loyalty_discount"] == str(expected_loyalty)
        assert breakdown["shipping"]["shipping_zone_id"] == str(shipping_zone_id)
        assert set(breakdown["ignored_client_fields"]) >= {"subtotal", "couponDiscount", "loyaltyDiscount", "total"}

        item = (
            await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))
        ).scalar_one()
        assert item.variant_id == variant_id
        assert item.unit_price == Decimal("1000.55")
        assert item.total_price == expected_subtotal
        assert item.extra_data["pricing_snapshot"]["unit_price"] == "1000.55"

        coupon_usage = MODEL_BY_TABLE["coupon_usage"]
        usage_count = (
            await session.execute(
                select(func.count()).select_from(coupon_usage).where(coupon_usage.order_id == order_id)
            )
        ).scalar_one()
        assert usage_count == 1

        loyalty_model = MODEL_BY_TABLE["user_loyalty"]
        loyalty = (
            await session.execute(select(loyalty_model).where(loyalty_model.user_id == customer_id))
        ).scalar_one()
        assert loyalty.balance == Decimal("149.80")

        points_tx = MODEL_BY_TABLE["points_transactions"]
        points_total = (
            await session.execute(
                select(func.coalesce(func.sum(points_tx.amount), 0)).where(points_tx.order_id == order_id)
            )
        ).scalar_one()
        assert points_total == expected_loyalty

        payments_model = MODEL_BY_TABLE["payments"]
        paid_total = (
            await session.execute(
                select(func.coalesce(func.sum(payments_model.amount), 0)).where(
                    payments_model.order_id == order_id,
                    payments_model.status == "approved",
                )
            )
        ).scalar_one()
        assert paid_total == expected_total

        refunds_model = MODEL_BY_TABLE["refunds"]
        refund_total = (
            await session.execute(
                select(func.coalesce(func.sum(refunds_model.amount), 0)).where(
                    refunds_model.order_id == order_id,
                    refunds_model.status.in_(["completed", "succeeded", "provider_succeeded", "manual_completed"]),
                )
            )
        ).scalar_one()
        assert refund_total == Decimal("0")

    reconciliation = await reconcile_financial_integrity(order_ids=[order_id])
    assert reconciliation["status"] == "pass", reconciliation["issues"]
    assert reconciliation["summary"]["orders_checked"] == 1
    assert reconciliation["summary"]["totals"]["orders"] == str(expected_total)
    assert reconciliation["summary"]["totals"]["paid"] == str(expected_total)
    assert reconciliation["summary"]["totals"]["refunded"] == "0.00"
