from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Order, OrderItem, Product, Profile, User, UserCart, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio(loop_scope="module")


PREFIX = "CODEX_CONCURRENCY_TEST"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _checkout_body(shipping_zone_id: uuid.UUID, note: str = "concurrency-checkout") -> dict[str, object]:
    return {
        "paymentMethod": "cash",
        "shippingCost": 999999,
        "shippingZoneId": str(shipping_zone_id),
        "shippingAddress": {
            "recipientName": "Concurrency Customer",
            "phone": "+967711111111",
            "governorate": "Amanat Al Asimah",
            "city": "Sanaa",
            "address": note,
            "shippingZoneId": str(shipping_zone_id),
        },
    }


async def _seed_user(email_suffix: str, role: str = "customer") -> tuple[str, str, uuid.UUID]:
    password = "ValidPass123"
    email = f"{PREFIX.lower()}_{email_suffix}_{uuid.uuid4().hex[:10]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"{PREFIX}_{email_suffix}"))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return email, password, user.id


async def _seed_product(stock: int, suffix: str) -> uuid.UUID:
    async with SessionFactory() as session:
        product = Product(
            name=f"{PREFIX}_Product_{suffix}_{uuid.uuid4().hex[:8]}",
            sku=f"CODEX-CONC-{suffix}-{uuid.uuid4().hex[:8]}",
            price=100,
            stock_quantity=stock,
            track_inventory=True,
            is_active=True,
            approval_status="approved",
        )
        session.add(product)
        await session.commit()
        return product.id


async def _seed_shipping_zone(suffix: str, fee: int = 0) -> uuid.UUID:
    async with SessionFactory() as session:
        shipping_model = MODEL_BY_TABLE["shipping_zones"]
        zone = shipping_model(
            name=f"{PREFIX}_Zone_{suffix}",
            fee=fee,
            status="active",
            is_active=True,
        )
        session.add(zone)
        await session.commit()
        return zone.id


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    return _headers(body["access_token"])


async def _add_cart(client: AsyncClient, headers: dict[str, str], product_id: uuid.UUID, quantity: int = 1) -> None:
    response = await client.post(
        "/cart",
        headers=headers,
        json={"productId": str(product_id), "quantity": quantity},
    )
    assert response.status_code in {200, 201}, response.text


async def _order_count_for_key(key: str) -> int:
    async with SessionFactory() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Order).where(Order.idempotency_key == key)
                )
            ).scalar_one()
        )


async def test_repeated_checkout_key_replays_same_order_without_extra_stock_decrement() -> None:
    suffix = uuid.uuid4().hex[:10]
    email, password, _ = await _seed_user(f"replay_{suffix}")
    product_id = await _seed_product(stock=2, suffix=f"replay_{suffix}")
    shipping_zone_id = await _seed_shipping_zone(f"replay_{suffix}")
    idempotency_key = f"{PREFIX}_checkout_replay_{suffix}"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login(client, email, password)
        await _add_cart(client, headers, product_id)
        checkout_headers = {**headers, "Idempotency-Key": idempotency_key}
        first = await client.post("/orders/checkout", headers=checkout_headers, json=_checkout_body(shipping_zone_id))
        second = await client.post("/orders/checkout", headers=checkout_headers, json=_checkout_body(shipping_zone_id))

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["idempotency_replayed"] is True
    assert await _order_count_for_key(idempotency_key) == 1

    async with SessionFactory() as session:
        stored_product = await session.get(Product, product_id)
        assert stored_product is not None
        assert stored_product.stock_quantity == 1


async def test_concurrent_double_checkout_same_key_creates_one_order() -> None:
    suffix = uuid.uuid4().hex[:10]
    email, password, _ = await _seed_user(f"double_{suffix}")
    product_id = await _seed_product(stock=5, suffix=f"double_{suffix}")
    shipping_zone_id = await _seed_shipping_zone(f"double_{suffix}")
    idempotency_key = f"{PREFIX}_checkout_double_{suffix}"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", timeout=20) as client:
        headers = await _login(client, email, password)
        await _add_cart(client, headers, product_id)
        checkout_headers = {**headers, "Idempotency-Key": idempotency_key}

        first, second = await asyncio.gather(
            client.post("/orders/checkout", headers=checkout_headers, json=_checkout_body(shipping_zone_id, "double-a")),
            client.post("/orders/checkout", headers=checkout_headers, json=_checkout_body(shipping_zone_id, "double-a")),
        )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 201], f"{first.status_code}:{first.text} / {second.status_code}:{second.text}"
    assert first.json()["id"] == second.json()["id"]
    assert await _order_count_for_key(idempotency_key) == 1

    async with SessionFactory() as session:
        stored_product = await session.get(Product, product_id)
        assert stored_product is not None
        assert stored_product.stock_quantity == 4


async def test_same_idempotency_key_with_different_payload_is_rejected() -> None:
    suffix = uuid.uuid4().hex[:10]
    email, password, _ = await _seed_user(f"conflict_payload_{suffix}")
    product_id = await _seed_product(stock=3, suffix=f"conflict_payload_{suffix}")
    shipping_zone_id = await _seed_shipping_zone(f"conflict_payload_{suffix}")
    idempotency_key = f"{PREFIX}_checkout_payload_conflict_{suffix}"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login(client, email, password)
        await _add_cart(client, headers, product_id)
        checkout_headers = {**headers, "Idempotency-Key": idempotency_key}
        first = await client.post("/orders/checkout", headers=checkout_headers, json=_checkout_body(shipping_zone_id, "payload-a"))
        conflict = await client.post("/orders/checkout", headers=checkout_headers, json=_checkout_body(shipping_zone_id, "payload-b"))

    assert first.status_code == 201, first.text
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == "idempotency_key_conflict"
    assert await _order_count_for_key(idempotency_key) == 1


async def test_same_idempotency_key_from_different_user_is_rejected() -> None:
    suffix = uuid.uuid4().hex[:10]
    first_email, first_password, _ = await _seed_user(f"owner_{suffix}")
    second_email, second_password, _ = await _seed_user(f"intruder_{suffix}")
    first_product = await _seed_product(stock=3, suffix=f"owner_{suffix}")
    second_product = await _seed_product(stock=3, suffix=f"intruder_{suffix}")
    shipping_zone_id = await _seed_shipping_zone(f"cross_user_{suffix}")
    idempotency_key = f"{PREFIX}_checkout_cross_user_{suffix}"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first_headers = await _login(client, first_email, first_password)
        await _add_cart(client, first_headers, first_product)
        first = await client.post(
            "/orders/checkout",
            headers={**first_headers, "Idempotency-Key": idempotency_key},
            json=_checkout_body(shipping_zone_id, "cross-user"),
        )
        second_headers = await _login(client, second_email, second_password)
        await _add_cart(client, second_headers, second_product)
        conflict = await client.post(
            "/orders/checkout",
            headers={**second_headers, "Idempotency-Key": idempotency_key},
            json=_checkout_body(shipping_zone_id, "cross-user"),
        )

    assert first.status_code == 201, first.text
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == "idempotency_key_conflict"
    assert await _order_count_for_key(idempotency_key) == 1


async def test_last_item_purchase_allows_one_success_only() -> None:
    suffix = uuid.uuid4().hex[:10]
    first_email, first_password, _ = await _seed_user(f"last_a_{suffix}")
    second_email, second_password, _ = await _seed_user(f"last_b_{suffix}")
    product_id = await _seed_product(stock=1, suffix=f"last_item_{suffix}")
    shipping_zone_id = await _seed_shipping_zone(f"last_item_{suffix}")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", timeout=20) as client:
        first_headers = await _login(client, first_email, first_password)
        second_headers = await _login(client, second_email, second_password)
        await _add_cart(client, first_headers, product_id)
        await _add_cart(client, second_headers, product_id)

        first, second = await asyncio.gather(
            client.post(
                "/orders/checkout",
                headers={**first_headers, "Idempotency-Key": f"{PREFIX}_last_a_{suffix}"},
                json=_checkout_body(shipping_zone_id, "last-a"),
            ),
            client.post(
                "/orders/checkout",
                headers={**second_headers, "Idempotency-Key": f"{PREFIX}_last_b_{suffix}"},
                json=_checkout_body(shipping_zone_id, "last-b"),
            ),
        )

    assert sorted([first.status_code, second.status_code]) == [201, 409], (
        f"{first.status_code}:{first.text} / {second.status_code}:{second.text}"
    )

    async with SessionFactory() as session:
        stored_product = await session.get(Product, product_id)
        assert stored_product is not None
        assert stored_product.stock_quantity == 0
        order_items = int(
            (
                await session.execute(
                    select(func.count()).select_from(OrderItem).where(OrderItem.product_id == product_id)
                )
            ).scalar_one()
        )
        assert order_items == 1


async def test_concurrent_cart_add_merges_one_no_variant_line() -> None:
    suffix = uuid.uuid4().hex[:10]
    email, password, user_id = await _seed_user(f"cart_merge_{suffix}")
    product_id = await _seed_product(stock=20, suffix=f"cart_merge_{suffix}")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", timeout=20) as client:
        headers = await _login(client, email, password)
        first, second = await asyncio.gather(
            client.post("/cart", headers=headers, json={"productId": str(product_id), "quantity": 1}),
            client.post("/cart", headers=headers, json={"productId": str(product_id), "quantity": 1}),
        )

    assert first.status_code in {200, 201}, first.text
    assert second.status_code in {200, 201}, second.text

    async with SessionFactory() as session:
        rows = (
            await session.execute(
                select(UserCart).where(UserCart.user_id == user_id, UserCart.product_id == product_id)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].quantity == 2


async def test_payment_receipt_double_approval_updates_once() -> None:
    suffix = uuid.uuid4().hex[:10]
    first_admin_email, first_admin_password, _ = await _seed_user(f"receipt_admin_a_{suffix}", role="admin")
    second_admin_email, second_admin_password, _ = await _seed_user(f"receipt_admin_b_{suffix}", role="admin")
    _, _, customer_id = await _seed_user(f"receipt_customer_{suffix}")
    receipt_model = MODEL_BY_TABLE["payment_receipts"]
    audit_model = MODEL_BY_TABLE["audit_logs"]

    async with SessionFactory() as session:
        order = Order(
            order_number=f"CODEX-CONC-RECEIPT-{suffix}",
            user_id=customer_id,
            status="pending",
            subtotal=100,
            total=100,
            payment_method="transfer",
            payment_status="pending",
            shipping_address={"city": "Sanaa"},
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                product_name=f"CODEX-CONC-RECEIPT-ITEM-{suffix}",
                quantity=1,
                unit_price=100,
                total_price=100,
            )
        )
        receipt = receipt_model(
            order_id=order.id,
            user_id=customer_id,
            status="pending",
            image_url="/uploads/payment-receipts/codex-test.png",
            amount=100,
        )
        session.add(receipt)
        await session.commit()
        order_id = order.id
        receipt_id = receipt.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", timeout=20) as client:
        first_headers = await _login(client, first_admin_email, first_admin_password)
        second_headers = await _login(client, second_admin_email, second_admin_password)
        first, second = await asyncio.gather(
            client.post(f"/payments/{receipt_id}/review", headers=first_headers, json={"status": "approved"}),
            client.post(f"/payments/{receipt_id}/review", headers=second_headers, json={"status": "approved"}),
        )
        rejected_transition = await client.post(
            f"/payments/{receipt_id}/review",
            headers=second_headers,
            json={"status": "rejected"},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert {bool(first.json().get("idempotency_replayed")), bool(second.json().get("idempotency_replayed"))} == {False, True}
    assert rejected_transition.status_code == 409, rejected_transition.text

    async with SessionFactory() as session:
        receipt = await session.get(receipt_model, receipt_id)
        order = await session.get(Order, order_id)
        audit_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(audit_model).where(
                        audit_model.type == "payment_receipt_reviewed",
                        audit_model.extra_data["payment_receipt_id"].astext == str(receipt_id),
                    )
                )
            ).scalar_one()
        )
    assert receipt is not None
    assert receipt.status == "approved"
    assert order is not None
    assert order.payment_status == "paid"
    assert audit_count == 1
