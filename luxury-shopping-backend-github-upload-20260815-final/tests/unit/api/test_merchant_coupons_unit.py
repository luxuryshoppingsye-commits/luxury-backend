from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.api.routes import operations
from app.models import MODEL_BY_TABLE
from app.models.domain import Product
from app.services import financial_calculator as fc


@compiles(JSONB, "sqlite")
def sqlite_json(element, compiler, **kwargs):
    return "JSON"


class _LocalSession:
    def __init__(self, database):
        self.database = database
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.database.execute(statement)

    async def get(self, model, identity, **kwargs):
        return self.database.get(model, identity)

    def add(self, row):
        self.database.add(row)

    async def flush(self):
        self.database.flush()

    async def commit(self):
        self.database.commit()


@pytest.fixture
def site(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    for model in (Product, MODEL_BY_TABLE["partner_coupons"], MODEL_BY_TABLE["coupons"]):
        model.__table__.create(engine)
    database = Session(engine, expire_on_commit=False)
    local = _LocalSession(database)
    merchant_id, customer_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    actor = {"id": merchant_id, "roles": {"partner"}}
    dress = Product(id=uuid.uuid4(), name="فستان ميدي", price=Decimal("30000"), partner_id=merchant_id)
    bag = Product(id=uuid.uuid4(), name="حقيبة جلدية", price=Decimal("20000"), partner_id=other_id)
    database.add_all([dress, bag])
    database.commit()
    cart = [(SimpleNamespace(quantity=1), dress, None, Decimal("30000")),
            (SimpleNamespace(quantity=1), bag, None, Decimal("20000"))]

    async def current_user():
        if actor["id"] is None:
            raise HTTPException(status_code=401, detail="authentication_required")
        return SimpleNamespace(id=actor["id"])

    async def roles():
        return actor["roles"]

    async def session():
        return local

    async def cart_lines(*args, **kwargs):
        return cart, sum(unit * item.quantity for item, _, _, unit in cart), Decimal("0")

    app = FastAPI()
    app.include_router(operations.router)
    app.dependency_overrides[operations.current_user] = current_user
    app.dependency_overrides[operations.user_roles] = roles
    app.dependency_overrides[operations.get_session] = session
    monkeypatch.setattr(operations, "_partner_coupon_store_name", AsyncMock(return_value="متجر سارة"))
    monkeypatch.setattr(operations, "_notify_coupon_customers", AsyncMock(return_value=0))
    monkeypatch.setattr(operations, "_validated_cart_lines", cart_lines)
    monkeypatch.setattr(fc, "_shipping_configuration", AsyncMock(return_value={
        "default_fee": 5000, "free_shipping_threshold": 50000,
    }))
    yield SimpleNamespace(app=app, db=database, local=local, actor=actor, merchant_id=merchant_id,
                          customer_id=customer_id, other_id=other_id, dress=dress, bag=bag, cart=cart)
    database.close()
    engine.dispose()


_CAMPAIGN = {"title": "خصم الفساتين", "code": "SAVE3220", "discount_type": "percentage",
             "discount_value": 20, "minimum_order_amount": 100, "scope": "all", "audience": "all"}


@pytest.mark.asyncio
async def test_create_list_count_and_validate_same_merchant_coupon(site):
    async with AsyncClient(transport=ASGITransport(app=site.app), base_url="http://localhost") as client:
        created = await client.post("/partner/coupons", json=_CAMPAIGN)
        assert created.status_code == 201, created.text
        record = created.json()["data"]
        assert record["customer_coupon_id"]
        listed = (await client.get("/partner/coupons")).json()["data"]
        assert [row["code"] for row in listed] == ["SAVE3220"]
        assert (await client.get("/partner/coupons/count")).json() == {"data": {"count": 1}}
        site.actor.update(id=site.customer_id, roles={"customer"})
        validated = await client.post("/coupons/validate", json={"code": "save3220", "subtotal": 1})
        assert validated.status_code == 200, validated.text
        assert validated.json()["coupon_id"] == record["customer_coupon_id"]
        # The second store's 20,000 YER never receives the merchant's discount.
        assert validated.json()["discount_amount"] == "6000.00"
        final = await fc.calculate_checkout_financials(
            site.local, user_id=site.customer_id, subtotal=Decimal("30000"),
            body={"couponCode": "SAVE3220", "total": 1, "couponDiscount": 1},
            coupon_lines=[(site.dress, 1, Decimal("30000"))],
            apply_free_shipping=True,
        )
        assert final.coupon_id == record["customer_coupon_id"]
        assert final.coupon_discount == Decimal("6000.00")
        assert final.shipping_total == Decimal("5000.00")
        assert final.total == Decimal("29000.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("dangling,active", [(False, True), (True, True), (False, False), (True, False)])
async def test_save_repairs_missing_customer_record_and_preserves_activation(site, dangling, active):
    model = MODEL_BY_TABLE["partner_coupons"]
    old = model(id=uuid.uuid4(), partner_id=site.merchant_id, code="SAVE3220", amount=20,
                is_active=active, status="active" if active else "inactive", extra_data={
                    **_CAMPAIGN, **({"customer_coupon_id": str(uuid.uuid4())} if dangling else {})})
    site.db.add(old)
    site.db.commit()
    async with AsyncClient(transport=ASGITransport(app=site.app), base_url="http://localhost") as client:
        saved = await client.patch(f"/partner/coupons/{old.id}", json={"discount_value": 20})
        assert saved.status_code == 200, saved.text
        linked_id = uuid.UUID(saved.json()["data"]["customer_coupon_id"])
        linked = site.db.get(MODEL_BY_TABLE["coupons"], linked_id)
        assert linked.is_active is active
        assert linked.extra_data["partner_id"] == str(site.merchant_id)
        assert linked.extra_data["partner_coupon_id"] == str(old.id)
        assert (await client.get("/partner/coupons/count")).json()["data"]["count"] == 1
        site.actor.update(id=site.customer_id, roles={"customer"})
        validated = await client.post("/coupons/validate", json={"code": "SAVE3220"})
        assert validated.status_code == (200 if active else 404)
        if active:
            assert validated.json()["discount_amount"] == "6000.00"


@pytest.mark.asyncio
async def test_count_excludes_deleted_and_other_merchants_without_a_row_limit(site):
    model = MODEL_BY_TABLE["partner_coupons"]
    for index in range(1003):
        site.db.add(model(partner_id=site.merchant_id, code=f"DRESS{index}", extra_data={}))
    site.db.add(model(partner_id=site.other_id, code="BAG20", extra_data={}))
    site.db.add(model(partner_id=site.merchant_id, code="DRESS30", deleted_at=datetime.now(timezone.utc)))
    site.db.commit()
    async with AsyncClient(transport=ASGITransport(app=site.app), base_url="http://localhost") as client:
        counted = await client.get("/partner/coupons/count?partner_id=" + str(site.other_id))
        assert counted.json() == {"data": {"count": 1003}}
        assert site.local.statements[-1]._limit_clause is None


@pytest.mark.asyncio
async def test_merchant_coupon_does_not_apply_to_another_store(site):
    async with AsyncClient(transport=ASGITransport(app=site.app), base_url="http://localhost") as client:
        assert (await client.post("/partner/coupons", json=_CAMPAIGN)).status_code == 201
        site.actor.update(id=site.customer_id, roles={"customer"})
        site.cart[:] = [site.cart[1]]
        result = await client.post("/coupons/validate", json={"code": "SAVE3220"})
        assert result.status_code == 409
        assert result.json()["detail"] == "coupon_not_applicable"


@pytest.mark.asyncio
async def test_ownership_and_duplicate_coupon_code_remain_protected(site):
    async with AsyncClient(transport=ASGITransport(app=site.app), base_url="http://localhost") as client:
        created = (await client.post("/partner/coupons", json=_CAMPAIGN)).json()["data"]
        assert (await client.post("/partner/coupons", json=_CAMPAIGN)).status_code == 409
        foreign_target = {**_CAMPAIGN, "code": "DRESS10", "scope": "products", "product_ids": [str(site.bag.id)]}
        assert (await client.post("/partner/coupons", json=foreign_target)).status_code == 422
        site.actor["id"] = site.other_id
        assert (await client.patch(f"/partner/coupons/{created['id']}", json={"discount_value": 90})).status_code == 404
        assert (await client.get("/partner/coupons/count")).json()["data"]["count"] == 0


@pytest.mark.asyncio
async def test_coupon_counter_requires_merchant_authentication(site):
    async with AsyncClient(transport=ASGITransport(app=site.app), base_url="http://localhost") as client:
        site.actor.update(id=site.customer_id, roles={"customer"})
        assert (await client.get("/partner/coupons/count")).status_code == 403
        site.actor["id"] = None
        assert (await client.get("/partner/coupons/count")).status_code == 401
