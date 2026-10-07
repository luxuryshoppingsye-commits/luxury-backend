from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, DateTime, MetaData, String, Table, Uuid, create_engine

from app.api.routes import commerce
from app.models.domain import Order, OrderItem
from app.services.merchant_order_scope import merchant_order_count


class _SqlSession:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, statement):
        return self.connection.execute(statement)


@pytest.fixture
def ledger():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    orders = Table(
        Order.__tablename__, metadata,
        Column("id", Uuid, primary_key=True),
        Column("deleted_at", DateTime),
        Column("status", String),
    )
    items = Table(
        OrderItem.__tablename__, metadata,
        Column("id", Uuid, primary_key=True),
        Column("order_id", Uuid),
        Column("partner_id", Uuid),
        Column("product_name", String),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        yield _SqlSession(connection), orders, items
    engine.dispose()


def _seed(ledger, owners, *, deleted=False, status="pending"):
    session, orders, items = ledger
    order_id = uuid.uuid4()
    session.connection.execute(orders.insert(), {
        "id": order_id,
        "deleted_at": datetime(2026, 10, 6) if deleted else None,
        "status": status,
    })
    if owners:
        session.connection.execute(items.insert(), [
            {"id": uuid.uuid4(), "order_id": order_id, "partner_id": owner,
             "product_name": "فستان سهرة" if index == 0 else "حقيبة يد"}
            for index, owner in enumerate(owners)
        ])


@pytest.mark.asyncio
async def test_count_matches_merchant_order_scope_without_item_duplicates(ledger):
    merchant_a, merchant_b, merchant_c = [uuid.uuid4() for _ in range(3)]
    _seed(ledger, [merchant_a, merchant_a, merchant_b])
    _seed(ledger, [merchant_a], status="delivered")
    _seed(ledger, [merchant_a], status="cancelled")
    _seed(ledger, [merchant_b])
    _seed(ledger, [merchant_a], deleted=True)
    _seed(ledger, [None])
    _seed(ledger, [])
    session, _, _ = ledger
    assert await merchant_order_count(session, partner_id=merchant_a) == 3
    assert await merchant_order_count(session, partner_id=merchant_b) == 2
    assert await merchant_order_count(session, partner_id=merchant_c) == 0


@pytest.mark.asyncio
async def test_count_is_not_limited_to_a_page_of_orders(ledger):
    merchant = uuid.uuid4()
    session, orders, items = ledger
    order_ids = [uuid.uuid4() for _ in range(1205)]
    session.connection.execute(orders.insert(), [
        {"id": order_id, "deleted_at": None, "status": "pending"}
        for order_id in order_ids
    ])
    session.connection.execute(items.insert(), [
        {"id": uuid.uuid4(), "order_id": order_id,
         "partner_id": merchant, "product_name": "فستان سهرة"}
        for order_id in order_ids
    ])
    assert await merchant_order_count(session, partner_id=merchant) == 1205


@pytest.mark.asyncio
async def test_count_endpoint_uses_authenticated_merchant_and_rejects_other_roles(ledger):
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()
    _seed(ledger, [merchant_a, merchant_a, merchant_b])
    _seed(ledger, [merchant_a])
    actor = {"id": merchant_a, "roles": {"partner"}}
    app = FastAPI()
    app.include_router(commerce.router)

    async def current_user():
        if actor["id"] is None:
            raise HTTPException(status_code=401, detail="authentication_required")
        return SimpleNamespace(id=actor["id"])

    async def roles():
        return actor["roles"]

    async def session():
        return ledger[0]

    app.dependency_overrides[commerce.current_user] = current_user
    app.dependency_overrides[commerce.user_roles] = roles
    app.dependency_overrides[commerce.get_session] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get(f"/api/partner/orders/count?partner_id={merchant_b}")
        assert response.status_code == 200
        assert response.json() == {"data": {"count": 2}}
        actor["id"] = merchant_b
        assert (await client.get("/api/partner/orders/count")).json() == {"data": {"count": 1}}
        actor["roles"] = {"customer"}
        assert (await client.get("/api/partner/orders/count")).status_code == 403
        actor["id"] = None
        assert (await client.get("/api/partner/orders/count")).status_code == 401
