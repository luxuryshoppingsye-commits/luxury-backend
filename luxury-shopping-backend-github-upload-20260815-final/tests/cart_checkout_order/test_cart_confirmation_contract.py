from __future__ import annotations
import asyncio
import uuid
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from app.config import get_settings
from app.database import SessionFactory, engine
from app.main import app
from app.models import MODEL_BY_TABLE
from app.models.domain import Order, Product, Profile, User, UserCart, UserRole
from app.security.passwords import hash_password

pytestmark = pytest.mark.asyncio(loop_scope="module")

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def records():
    settings=get_settings()
    assert settings.is_test_environment and settings.allow_test_fixtures
    assert urlsplit(settings.database_url).hostname == "127.0.0.1"
    suffix=uuid.uuid4().hex[:8]
    people=[]
    async with SessionFactory() as s:
        for name in ("ghaya","reem"):
            person=User(email=f"{name}-{suffix}@gmail.com",password_hash=hash_password("Admin1234"),is_active=True)
            s.add(person);await s.flush()
            s.add(Profile(id=person.id,user_id=person.id,email=person.email,full_name=name))
            s.add(UserRole(user_id=person.id,role="customer"));people.append(person)
        zone=MODEL_BY_TABLE["shipping_zones"](name="صنعاء",fee=100,status="active",is_active=True)
        setting=MODEL_BY_TABLE["site_settings"]
        existing=(await s.execute(select(setting).where(setting.name=="payment_methods"))).scalar_one_or_none()
        if existing is None:
            existing=setting(name="payment_methods",extra_data={});s.add(existing)
        existing.extra_data={"configured_by_admin":True,"methods":[{"provider_key":"CASH_ON_DELIVERY","is_active":True}]}
        s.add(zone);await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://testserver") as client:
        headers=[]
        for person in people:
            r=await client.post("/auth/login",json={"email":person.email,"password":"Admin1234"})
            assert r.status_code==200,r.text
            headers.append({"Authorization":"Bearer "+r.json()["access_token"]})
        yield people,headers,client,zone.id

async def order_for(person, status):
    async with SessionFactory() as s:
        order=Order(user_id=person.id,order_number=f"INV-{uuid.uuid4().hex[:8]}",status=status,
            subtotal=7000,total=7000,shipping_total=0,discount_total=0,currency_code="YER",payment_status="pending",payment_method="cod")
        s.add(order);await s.commit();return order.id

@pytest.mark.parametrize("status",["shipped","out_for_delivery","delivering","delivered","completed"])
async def test_owner_confirmation_sets_delivery_once_without_changing_payment(records,status):
    people,headers,client,_=records;identity=await order_for(people[0],status)
    result=await client.post(f"/api/orders/{identity}/received",headers=headers[0],json={})
    assert result.status_code==200,result.text
    assert result.json()["order"]["status"]==("completed" if status=="completed" else "delivered")
    assert result.json()["customerReceived"] is True
    again=await client.post(f"/orders/{identity}/received",headers=headers[0],json={})
    assert again.status_code==200 and again.json()["idempotent"] is True,again.text
    async with SessionFactory() as s:
        stored=await s.get(Order,identity)
        assert stored.extra_data["customer_received_by"]==str(people[0].id)
        assert stored.payment_status=="pending" and stored.total==Decimal("7000")
        history=MODEL_BY_TABLE["order_status_history"]
        rows=list((await s.execute(select(history).where(history.order_id==identity))).scalars())
        assert sum(row.status=="customer_received" for row in rows)==1
        assert sum(row.status=="delivered" for row in rows)==(0 if status in {"delivered","completed"} else 1)

@pytest.mark.parametrize("status",["pending","preparing","ready_for_shipment","cancelled","returned"])
async def test_receipt_confirmation_rejects_unshipped_or_closed_orders(records,status):
    people,headers,client,_=records;identity=await order_for(people[0],status)
    result=await client.post(f"/orders/{identity}/received",headers=headers[0],json={})
    assert result.status_code==409,result.text
    async with SessionFactory() as s:
        stored=await s.get(Order,identity)
        assert stored.status==status and not stored.extra_data.get("customer_received_at")

async def test_other_customer_cannot_confirm_or_use_the_staff_status_route(records):
    people,headers,client,_=records;identity=await order_for(people[0],"shipped")
    result=await client.post(f"/orders/{identity}/received",headers=headers[1],json={})
    assert result.status_code==404,result.text
    result=await client.patch(f"/api/orders/{identity}/status",headers=headers[0],json={"status":"delivered"})
    assert result.status_code==403,result.text

async def seed_products(count=1,stock=3):
    async with SessionFactory() as s:
        products=[Product(name="فستان كتان بأكمام طويلة",sku=uuid.uuid4().hex,price=7000,stock_quantity=stock,track_inventory=True,is_active=True,approval_status="approved") for _ in range(count)]
        s.add_all(products);await s.commit();return [product.id for product in products]

async def test_cart_locks_products_in_one_batch_and_rejects_excess_stock(records):
    _,headers,client,_=records;products=await seed_products(4);queries=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        if "FROM products" in statement and "FOR UPDATE" in statement:queries.append(statement)
    event.listen(engine.sync_engine,"before_cursor_execute",capture)
    try:
        result=await client.put("/api/cart",headers=headers[0],json={"items":[{"productId":str(identity),"quantity":2} for identity in products]})
    finally:event.remove(engine.sync_engine,"before_cursor_execute",capture)
    assert result.status_code==200,result.text
    assert len(queries)==1,queries
    excessive=await client.put("/api/cart",headers=headers[0],json={"items":[{"productId":str(products[0]),"quantity":4}]})
    assert excessive.status_code==409,excessive.text
    async with SessionFactory() as s:
        rows=list((await s.execute(select(UserCart).where(UserCart.user_id==records[0][0].id))).scalars())
        assert len(rows)==4 and all(row.quantity==2 for row in rows)

async def test_concurrent_checkout_cannot_sell_the_same_last_item_twice(records):
    _,headers,client,zone_id=records;identity=(await seed_products(stock=1))[0]
    for auth in headers:
        r=await client.put("/api/cart",headers=auth,json={"items":[{"productId":str(identity),"quantity":1}]})
        assert r.status_code==200,r.text
    body={"paymentMethod":"cash_on_delivery","shippingZoneId":str(zone_id),"shippingAddress":{"fullName":"غاية أحمد","phone":"777123456","governorate":"صنعاء","city":"صنعاء","address":"شارع حدة"}}
    results=await asyncio.gather(*[client.post("/orders/checkout",headers={**auth,"Idempotency-Key":str(uuid.uuid4())},json=body) for auth in headers])
    assert sorted(r.status_code for r in results)==[201,409],[r.text for r in results]
    async with SessionFactory() as s:
        product=await s.get(Product,identity);assert product.stock_quantity==0
        succeeded=next(r.json() for r in results if r.status_code==201)
        order_id=uuid.UUID(succeeded["id"])
        order=await s.get(Order,order_id)
        rows=list((await s.execute(select(UserCart).where(UserCart.user_id==order.user_id))).scalars())
        assert not rows

@pytest.mark.parametrize("items,status",[(["dress"],400),([{"productId":"placeholder","quantity":1}],422)])
async def test_invalid_cart_payload_does_not_discard_the_saved_cart(records,items,status):
    _,headers,client,_=records
    if items[0]=="dress":
        before=(await client.get("/api/cart",headers=headers[0])).json()
        result=await client.put("/api/cart",headers=headers[0],json={"items":items})
    else:
        product=(await seed_products())[0]
        item={"productId":str(product),"quantity":2}
        before=(await client.get("/api/cart",headers=headers[0])).json()
        result=await client.put("/api/cart",headers=headers[0],json={"items":[item,item]})
    assert result.status_code==status,result.text
    after=(await client.get("/api/cart",headers=headers[0])).json()
    assert before==after
