from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Order, OrderItem, Product, ProductVariant, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio(loop_scope="module")


PREFIX = "CART_ORDER_REMEDIATION"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(label: str, role: str = "customer") -> tuple[str, str, uuid.UUID]:
    password = "ValidPass123"
    email = f"{PREFIX.lower()}_{label}_{uuid.uuid4().hex[:8]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"{PREFIX} {label}"))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return email, password, user.id


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return _headers(response.json()["access_token"])


async def _seed_shipping_zone(label: str, fee: Decimal = Decimal("100.00")) -> uuid.UUID:
    async with SessionFactory() as session:
        model = MODEL_BY_TABLE["shipping_zones"]
        zone = model(
            name=f"{PREFIX} Zone {label}",
            status="active",
            fee=fee,
            is_active=True,
            sort_order=1,
        )
        session.add(zone)
        await session.commit()
        return zone.id


async def _seed_product(
    label: str,
    *,
    price: Decimal = Decimal("800.00"),
    original_price: Decimal | None = None,
    stock: int = 10,
    active: bool = True,
    approval_status: str = "approved",
    deleted: bool = False,
) -> uuid.UUID:
    async with SessionFactory() as session:
        product = Product(
            name=f"{PREFIX} Product {label}",
            sku=f"REM-{label}-{uuid.uuid4().hex[:8]}",
            price=price,
            original_price=original_price,
            stock_quantity=stock,
            track_inventory=True,
            is_active=active,
            approval_status=approval_status,
            deleted_at=datetime.now(timezone.utc) if deleted else None,
        )
        session.add(product)
        await session.commit()
        return product.id


async def _seed_variant(
    product_id: uuid.UUID,
    label: str,
    *,
    price: Decimal = Decimal("700.00"),
    original_price: Decimal | None = None,
    stock: int = 5,
    active: bool = True,
) -> uuid.UUID:
    async with SessionFactory() as session:
        variant = ProductVariant(
            product_id=product_id,
            sku=f"REM-VAR-{label}-{uuid.uuid4().hex[:8]}",
            size="M",
            color="Black",
            price=price,
            original_price=original_price,
            stock_quantity=stock,
            is_active=active,
        )
        session.add(variant)
        await session.commit()
        return variant.id


def _checkout_body(shipping_zone_id: uuid.UUID, *, coupon_code: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "paymentMethod": "cash",
        "shippingCost": "999999.99",
        "shippingZoneId": str(shipping_zone_id),
        "shippingAddress": {
            "recipientName": "Checkout Customer",
            "phone": "+967711111111",
            "governorate": "Amanat Al Asimah",
            "city": "Sanaa",
            "address": "Remediation Street",
            "shippingZoneId": str(shipping_zone_id),
        },
    }
    if coupon_code:
        body["couponCode"] = coupon_code
    return body


async def _create_checkout_order(client: AsyncClient, headers: dict[str, str], product_id: uuid.UUID, shipping_zone_id: uuid.UUID) -> dict:
    cart = await client.post("/cart", headers=headers, json={"productId": str(product_id), "quantity": 1})
    assert cart.status_code == 201, cart.text
    checkout = await client.post(
        "/orders/checkout",
        headers={**headers, "Idempotency-Key": f"{PREFIX}_{uuid.uuid4().hex}"},
        json=_checkout_body(shipping_zone_id),
    )
    assert checkout.status_code == 201, checkout.text
    return checkout.json()


async def test_cart_holds_out_of_stock_products_but_rejects_non_public_products_and_bad_quantities() -> None:
    suffix = uuid.uuid4().hex[:8]
    email, password, _ = await _seed_user(f"eligibility_{suffix}")
    approved_id = await _seed_product(f"approved_{suffix}")
    out_of_stock_id = await _seed_product(f"out_of_stock_{suffix}", stock=0)
    pending_id = await _seed_product(f"pending_{suffix}", approval_status="pending")
    inactive_id = await _seed_product(f"inactive_{suffix}", active=False)
    deleted_id = await _seed_product(f"deleted_{suffix}", deleted=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login(client, email, password)
        approved = await client.post("/cart", headers=headers, json={"productId": str(approved_id), "quantity": 1})
        assert approved.status_code == 201, approved.text

        out_of_stock_cart = await client.post(
            "/cart",
            headers=headers,
            json={"productId": str(out_of_stock_id), "quantity": 1},
        )
        assert out_of_stock_cart.status_code == 201, out_of_stock_cart.text
        cart_read = await client.get("/cart", headers=headers)
        held_line = next(
            row for row in cart_read.json() if row["product_id"] == str(out_of_stock_id)
        )
        assert held_line["is_available_for_checkout"] is False
        assert held_line["availability_error"] == "insufficient_stock"

        out_of_stock_wishlist = await client.post(
            "/wishlist",
            headers=headers,
            json={"productId": str(out_of_stock_id)},
        )
        assert out_of_stock_wishlist.status_code == 201, out_of_stock_wishlist.text
        assert out_of_stock_wishlist.json()["product_id"] == str(out_of_stock_id)

        pending = await client.post("/cart", headers=headers, json={"productId": str(pending_id), "quantity": 1})
        assert pending.status_code == 409 and pending.json()["detail"] == "product_not_approved"

        inactive = await client.post("/cart", headers=headers, json={"productId": str(inactive_id), "quantity": 1})
        assert inactive.status_code == 409 and inactive.json()["detail"] == "product_inactive"

        deleted = await client.post("/cart", headers=headers, json={"productId": str(deleted_id), "quantity": 1})
        assert deleted.status_code == 404 and deleted.json()["detail"] == "product_not_available"

        wishlist = await client.post("/wishlist", headers=headers, json={"productId": str(pending_id)})
        assert wishlist.status_code == 409 and wishlist.json()["detail"] == "product_not_approved"

        for bad_quantity, expected_status in [(0, 400), (-1, 400), ("1.5", 400), (100000, 409)]:
            response = await client.post(
                "/cart",
                headers=headers,
                json={"productId": str(approved_id), "quantity": bad_quantity},
            )
            assert response.status_code == expected_status, response.text


async def test_variant_mismatch_inactive_and_checkout_revalidates_invalidated_cart() -> None:
    suffix = uuid.uuid4().hex[:8]
    email, password, _ = await _seed_user(f"variant_{suffix}")
    shipping_zone_id = await _seed_shipping_zone(f"variant_{suffix}")
    product_id = await _seed_product(f"variant_product_{suffix}", stock=5)
    other_product_id = await _seed_product(f"variant_other_{suffix}", stock=5)
    inactive_variant_id = await _seed_variant(product_id, f"inactive_{suffix}", active=False)
    other_variant_id = await _seed_variant(other_product_id, f"other_{suffix}")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login(client, email, password)
        mismatch = await client.post(
            "/cart",
            headers=headers,
            json={"productId": str(product_id), "variantId": str(other_variant_id), "quantity": 1},
        )
        assert mismatch.status_code == 409 and mismatch.json()["detail"] == "variant_product_mismatch"

        inactive = await client.post(
            "/cart",
            headers=headers,
            json={"productId": str(product_id), "variantId": str(inactive_variant_id), "quantity": 1},
        )
        assert inactive.status_code == 409 and inactive.json()["detail"] == "variant_not_active"

        cart = await client.post("/cart", headers=headers, json={"productId": str(product_id), "quantity": 1})
        assert cart.status_code == 201, cart.text

        async with SessionFactory() as session:
            product = await session.get(Product, product_id)
            assert product is not None
            product.is_active = False
            await session.commit()

        cart_read = await client.get("/cart", headers=headers)
        assert cart_read.status_code == 200
        assert cart_read.json()[0]["is_available_for_checkout"] is False
        assert cart_read.json()[0]["availability_error"] == "product_inactive"

        checkout = await client.post(
            "/orders/checkout",
            headers={**headers, "Idempotency-Key": f"{PREFIX}_invalidated_{suffix}"},
            json=_checkout_body(shipping_zone_id),
        )
        assert checkout.status_code == 409 and checkout.json()["detail"] == "product_inactive"


async def test_checkout_preview_matches_checkout_and_ignores_client_financial_fields() -> None:
    suffix = uuid.uuid4().hex[:8]
    email, password, _ = await _seed_user(f"pricing_{suffix}")
    shipping_zone_id = await _seed_shipping_zone(f"pricing_{suffix}", fee=Decimal("100.00"))
    product_id = await _seed_product(
        f"pricing_{suffix}",
        price=Decimal("800.00"),
        original_price=Decimal("1000.00"),
        stock=5,
    )
    async with SessionFactory() as session:
        coupon_model = MODEL_BY_TABLE["coupons"]
        coupon = coupon_model(
            code=f"PRICE{suffix.upper()}",
            title="Pricing coupon",
            status="active",
            amount=Decimal("100.00"),
            is_active=True,
            extra_data={"discount_type": "fixed", "discount_value": "100.00"},
        )
        session.add(coupon)
        await session.commit()
        coupon_code = coupon.code

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login(client, email, password)
        cart = await client.post("/cart", headers=headers, json={"productId": str(product_id), "quantity": 2})
        assert cart.status_code == 201, cart.text

        body = _checkout_body(shipping_zone_id, coupon_code=coupon_code)
        preview = await client.post("/checkout/preview", headers=headers, json=body)
        assert preview.status_code == 200, preview.text
        assert Decimal(preview.json()["subtotal"]) == Decimal("2000.00")
        assert Decimal(preview.json()["product_discount"]) == Decimal("400.00")
        assert Decimal(preview.json()["coupon_discount"]) == Decimal("100.00")
        assert Decimal(preview.json()["shipping_cost"]) == Decimal("100.00")
        assert Decimal(preview.json()["total"]) == Decimal("1600.00")

        checkout = await client.post(
            "/orders/checkout",
            headers={**headers, "Idempotency-Key": f"{PREFIX}_pricing_{suffix}"},
            json={**body, "total": "1.00", "subtotal": "1.00", "couponDiscount": "999.00"},
        )
        assert checkout.status_code == 201, checkout.text
        assert Decimal(checkout.json()["total"]) == Decimal(preview.json()["total"])
        assert Decimal(checkout.json()["shipping_total"]) == Decimal("100.00")


async def test_manual_order_uses_variant_price_stock_and_rejects_pending_product() -> None:
    suffix = uuid.uuid4().hex[:8]
    admin_email, admin_password, _ = await _seed_user(f"manual_admin_{suffix}", "admin")
    _, _, customer_id = await _seed_user(f"manual_customer_{suffix}", "customer")
    shipping_zone_id = await _seed_shipping_zone(f"manual_{suffix}", fee=Decimal("75.00"))
    product_id = await _seed_product(f"manual_{suffix}", price=Decimal("900.00"), stock=8)
    variant_id = await _seed_variant(product_id, f"manual_{suffix}", price=Decimal("650.00"), stock=3)
    pending_id = await _seed_product(f"manual_pending_{suffix}", approval_status="pending")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_headers = await _login(client, admin_email, admin_password)
        body = {
            "customerId": str(customer_id),
            "paymentMethod": "cash",
            "shippingZoneId": str(shipping_zone_id),
            "shippingAddress": {
                "recipientName": "Manual Customer",
                "phone": "+967711222444",
                "governorate": "Amanat Al Asimah",
                "city": "Sanaa",
                "address": "Manual Street",
                "shippingZoneId": str(shipping_zone_id),
            },
            "items": [{"productId": str(product_id), "variantId": str(variant_id), "quantity": 2}],
        }
        created = await client.post(
            "/admin/manual-order",
            headers={**admin_headers, "Idempotency-Key": f"{PREFIX}_manual_{suffix}"},
            json=body,
        )
        assert created.status_code == 200, created.text
        order_id = uuid.UUID(created.json()["id"])

        async with SessionFactory() as session:
            item = (
                await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))
            ).scalar_one()
            variant = await session.get(ProductVariant, variant_id)
            assert item.variant_id == variant_id
            assert item.unit_price == Decimal("650.00")
            assert item.total_price == Decimal("1300.00")
            assert variant is not None and variant.stock_quantity == 1

        rejected = await client.post(
            "/admin/manual-order",
            headers={**admin_headers, "Idempotency-Key": f"{PREFIX}_manual_reject_{suffix}"},
            json={**body, "items": [{"productId": str(pending_id), "quantity": 1}]},
        )
        assert rejected.status_code == 409 and rejected.json()["detail"] == "product_not_approved"


async def test_order_state_machine_courier_assignment_and_delivery_proof() -> None:
    suffix = uuid.uuid4().hex[:8]
    customer_email, customer_password, _ = await _seed_user(f"state_customer_{suffix}")
    admin_email, admin_password, _ = await _seed_user(f"state_admin_{suffix}", "admin")
    courier_email, courier_password, courier_id = await _seed_user(f"state_courier_{suffix}", "delivery")
    shipping_zone_id = await _seed_shipping_zone(f"state_{suffix}")
    product_id = await _seed_product(f"state_{suffix}", stock=5)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        customer_headers = await _login(client, customer_email, customer_password)
        admin_headers = await _login(client, admin_email, admin_password)
        courier_headers = await _login(client, courier_email, courier_password)
        order = await _create_checkout_order(client, customer_headers, product_id, shipping_zone_id)
        order_id = uuid.UUID(order["id"])

        invalid = await client.post(f"/orders/{order_id}/status", headers=admin_headers, json={"status": "shipped"})
        assert invalid.status_code == 409 and invalid.json()["detail"] == "invalid_order_transition"

        confirmed = await client.post(f"/orders/{order_id}/status", headers=admin_headers, json={"status": "confirmed"})
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"
        accepted = await client.post(f"/orders/{order_id}/status", headers=admin_headers, json={"status": "accepted"})
        assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
        ready = await client.post(f"/orders/{order_id}/status", headers=admin_headers, json={"status": "ready_for_shipment"})
        assert ready.status_code == 200 and ready.json()["status"] == "ready_for_shipment"

        unassigned = await client.post(f"/orders/{order_id}/status", headers=courier_headers, json={"status": "out_for_delivery"})
        assert unassigned.status_code == 403 and unassigned.json()["detail"] == "courier_not_assigned"

        async with SessionFactory() as session:
            assignment_model = MODEL_BY_TABLE["courier_assignments"]
            session.add(
                assignment_model(
                    courier_id=courier_id,
                    user_id=courier_id,
                    order_id=order_id,
                    status="assigned",
                )
            )
            await session.commit()

        out = await client.post(f"/orders/{order_id}/status", headers=courier_headers, json={"status": "out_for_delivery"})
        assert out.status_code == 200 and out.json()["status"] == "out_for_delivery"

        no_proof = await client.post(f"/orders/{order_id}/status", headers=courier_headers, json={"status": "delivered"})
        assert no_proof.status_code == 422 and no_proof.json()["detail"] == "delivery_proof_required"

        delivered = await client.post(
            f"/orders/{order_id}/status",
            headers=courier_headers,
            json={"status": "delivered", "deliveryProof": f"proof-{suffix}"},
        )
        assert delivered.status_code == 200 and delivered.json()["status"] == "delivered"

        async with SessionFactory() as session:
            stored = await session.get(Order, order_id)
            assert stored is not None and stored.status == "delivered"
