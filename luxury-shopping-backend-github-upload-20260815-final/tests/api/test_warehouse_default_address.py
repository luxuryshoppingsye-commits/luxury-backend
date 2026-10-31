import asyncio
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import User, Profile, UserRole, Product, ProductVariant, Order, OrderItem
from backend.app.security.passwords import hash_password
from backend.app.services.commerce_rules import deduct_order_inventory, restore_order_inventory


def record(table, **values):
    model = MODEL_BY_TABLE[table]
    fields = set(model.__table__.columns.keys())
    return model(**{k: v for k, v in values.items() if k in fields},
                 extra_data={k: v for k, v in values.items() if k not in fields})


async def customer(role="customer"):
    async with SessionFactory() as session:
        user = User(email=f"noura{uuid.uuid4().hex[:8]}@gmail.com", password_hash=hash_password("Admin1234"), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=user.email, full_name="نورة محمد", phone="777123456"))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return user.id, user.email


async def inventory(quantities=(2,), *, foreign=False, variant=False):
    user_id, _ = await customer()
    owner, other = uuid.uuid4(), uuid.uuid4()
    async with SessionFactory() as session:
        product = Product(name="فستان كتان", price=7000, stock_quantity=sum(quantities),
                          partner_id=owner, is_active=True, approval_status="approved", track_inventory=True)
        session.add(product)
        await session.flush()
        sku = ProductVariant(product_id=product.id, size="M", stock_quantity=sum(quantities), price=7000, is_active=True) if variant else None
        if sku:
            session.add(sku)
            await session.flush()
        locations = []
        for index, quantity in enumerate(quantities):
            warehouse = record("warehouses", name="مستودع المتجر" if index == 0 else "مستودع الفرع", is_active=True,
                               partner_id=str(other if foreign and index else owner))
            session.add(warehouse)
            await session.flush()
            location = record("inventory_locations", name="رصيد فستان كتان", is_active=True, quantity=quantity,
                              product_id=str(product.id), warehouse_id=str(warehouse.id))
            session.add(location)
            locations.append(location)
        await session.flush()
        await session.commit()
        return user_id, product.id, sku.id if sku else None, [row.id for row in locations]


async def purchase(user_id, product_id, variant_id, quantity):
    async with SessionFactory() as session:
        product = (await session.execute(select(Product).where(Product.id == product_id).with_for_update())).scalar_one()
        variant = await session.get(ProductVariant, variant_id, with_for_update=True) if variant_id else None
        order = Order(user_id=user_id, status="pending", subtotal=7000 * quantity, total=7000 * quantity)
        session.add(order)
        await session.flush()
        item = OrderItem(id=uuid.uuid4(), order_id=order.id, product_id=product.id, variant_id=variant_id,
                         product_name=product.name, partner_id=product.partner_id, quantity=quantity,
                         unit_price=7000, total_price=7000 * quantity)
        session.add(item)
        await deduct_order_inventory(session, product=product, variant=variant, item=item)
        await session.commit()
        return order.id


async def balances(product_id, variant_id, location_ids):
    async with SessionFactory() as session:
        product = await session.get(Product, product_id)
        sku = await session.get(ProductVariant, variant_id) if variant_id else None
        model = MODEL_BY_TABLE["inventory_locations"]
        return product.stock_quantity, sku.stock_quantity if sku else None, [(await session.get(model, id)).extra_data["quantity"] for id in location_ids]


@pytest.mark.asyncio
async def test_purchase_and_cancel_use_only_the_product_merchants_allocation():
    user, product, sku, locations = await inventory((2, 5), foreign=True)
    with pytest.raises(HTTPException) as denied:
        await purchase(user, product, sku, 3)
    assert denied.value.status_code == 409
    assert await balances(product, sku, locations) == (7, None, [2, 5])
    order_id = await purchase(user, product, sku, 2)
    assert await balances(product, sku, locations) == (5, None, [0, 5])
    async with SessionFactory() as session:
        order = await session.get(Order, order_id, with_for_update=True)
        await restore_order_inventory(session, order)
        await restore_order_inventory(session, order)
        await session.commit()
    assert await balances(product, sku, locations) == (7, None, [2, 5])


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [False, True])
async def test_split_warehouse_quantities_return_to_the_original_sources(variant):
    user, product, sku, locations = await inventory((1, 2), variant=variant)
    order_id = await purchase(user, product, sku, 3)
    assert await balances(product, sku, locations) == (0, 0 if variant else None, [0, 0])
    async with SessionFactory() as session:
        order = await session.get(Order, order_id, with_for_update=True)
        await restore_order_inventory(session, order)
        await session.commit()
    assert await balances(product, sku, locations) == (3, 3 if variant else None, [1, 2])


@pytest.mark.asyncio
async def test_concurrent_purchase_of_last_warehouse_piece_succeeds_once():
    user, product, sku, locations = await inventory((1,))
    results = await asyncio.gather(purchase(user, product, sku, 1), purchase(user, product, sku, 1), return_exceptions=True)
    assert sum(isinstance(row, uuid.UUID) for row in results) == 1
    assert sum(isinstance(row, HTTPException) and row.status_code == 409 for row in results) == 1
    assert await balances(product, sku, locations) == (0, None, [0])


@pytest.mark.asyncio
async def test_unassigned_product_stock_remains_supported():
    user, product, sku, locations = await inventory((3,))
    async with SessionFactory() as session:
        row = await session.get(MODEL_BY_TABLE["inventory_locations"], locations[0])
        await session.delete(row)
        await session.commit()
    order_id = await purchase(user, product, sku, 2)
    assert (await balances(product, sku, []))[0] == 1
    async with SessionFactory() as session:
        await restore_order_inventory(session, await session.get(Order, order_id))
        await session.commit()
    assert (await balances(product, sku, []))[0] == 3


@pytest.mark.asyncio
async def test_registration_address_is_initial_default(monkeypatch):
    from backend.app.api.routes import auth
    async def no_email(*args, **kwargs):
        return {"requires_verification": True, "delivery_status": "queued_test_provider"}
    monkeypatch.setattr(auth, "_queue_email_verification", no_email)
    email = f"ghaya{uuid.uuid4().hex[:8]}@gmail.com"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/auth/register-customer", json={"email": email, "password": "Admin1234", "fullName": "غاية أحمد", "phone": "777123456", "city": "صنعاء", "street": "شارع حدة", "addressDetails": "بجوار جامع السلام"})
    assert response.status_code == 201, response.text
    async with SessionFactory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        model = MODEL_BY_TABLE["customer_addresses"]
        rows = list((await session.execute(select(model).where(model.user_id == user.id))).scalars())
        assert len(rows) == 1 and rows[0].is_default is True
        assert rows[0].address == "شارع حدة - بجوار جامع السلام"


@pytest.mark.asyncio
async def test_first_address_becomes_default_and_checkout_addition_keeps_it():
    _, email = await customer()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        login = await client.post("/auth/login", json={"email": email, "password": "Admin1234"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        body = {"label": "البيت", "recipient_name": "نورة محمد", "phone": "777123456", "governorate": "أمانة العاصمة", "city": "صنعاء", "address": "شارع حدة", "is_default": False}
        first = await client.post("/api/profile/addresses", headers=headers, json=body)
        assert first.status_code == 201, first.text
        initial = first.json()["data"]
        assert initial["is_default"] is True
        second = await client.post("/api/profile/addresses", headers=headers, json={**body, "label": "العمل", "address": "شارع الزبيري"})
        assert second.status_code == 201, second.text
        added = second.json()["data"]
        assert added["id"] != initial["id"] and added["is_default"] is False
        listed = (await client.get("/api/profile/addresses", headers=headers)).json()["data"]
        assert len(listed) == 2
        assert [row["id"] for row in listed if row["is_default"]] == [initial["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_purchase_routes_and_cancellation_keep_warehouse_and_product_in_sync(manual):
    user_id, product_id, sku, locations = await inventory((2,))
    async with SessionFactory() as session:
        product = await session.get(Product, product_id)
        product.partner_id = None
        location = await session.get(MODEL_BY_TABLE["inventory_locations"], locations[0])
        warehouse = await session.get(MODEL_BY_TABLE["warehouses"], uuid.UUID(location.extra_data["warehouse_id"]))
        warehouse.extra_data = {}
        zone = record("shipping_zones", name="صنعاء", is_active=True, fee=100, governorate="أمانة العاصمة", city="صنعاء")
        session.add(zone)
        session.add(record("site_settings", name="payment_methods", configured_by_admin=True, methods=[{"provider_key":"CASH_ON_DELIVERY","is_active":True}]))
        email = (await session.get(User, user_id)).email
        await session.commit()
        zone_id = zone.id
    actor_email = (await customer("admin"))[1] if manual else email
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        login = await client.post("/auth/login", json={"email":actor_email,"password":"Admin1234"})
        assert login.status_code == 200, login.text
        headers = {"Authorization":"Bearer " + login.json()["access_token"], "Idempotency-Key":str(uuid.uuid4())}
        body = {"paymentMethod":"cash_on_delivery", "shippingZoneId":str(zone_id), "shippingAddress":{"recipientName":"نورة محمد","phone":"777123456","governorate":"أمانة العاصمة","city":"صنعاء","address":"شارع حدة"}}
        if manual:
            body.update({"customerId":str(user_id),"items":[{"productId":str(product_id),"quantity":1}]})
            endpoint = "/admin/manual-order"
        else:
            added = await client.post("/cart",headers=headers,json={"productId":str(product_id),"quantity":1})
            assert added.status_code in {200,201}, added.text
            endpoint = "/orders/checkout"
        placed = await client.post(endpoint, headers=headers, json=body)
        assert placed.status_code in {200,201}, placed.text
        assert await balances(product_id, sku, locations) == (1,None,[1])
        order_id = placed.json()["id"]
        replay = await client.post(endpoint, headers=headers, json=body)
        assert replay.status_code in {200,201}, replay.text
        assert replay.json()["id"] == order_id
        assert await balances(product_id, sku, locations) == (1,None,[1])
        cancelled = await client.post(f"/orders/{order_id}/status" if manual else f"/orders/{order_id}/cancel",headers=headers,json={"status":"cancelled"} if manual else {})
        assert cancelled.status_code == 200, cancelled.text
        assert await balances(product_id, sku, locations) == (2,None,[2])
