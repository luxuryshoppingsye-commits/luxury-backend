from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from backend.app import dependencies
from backend.app.api.routes import auth, commerce, operations
from backend.app.database import get_session
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Order, Profile, User
from backend.app.security import tokens


@compiles(JSONB, "sqlite")
def sqlite_json_column(element, compiler, **kwargs):
    return "JSON"


class LocalSession:
    """Execute endpoint SQL against an isolated, in-memory database only."""

    def __init__(self, session):
        self.session = session
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.session.execute(statement)

    async def get(self, model, identity):
        return self.session.get(model, identity)

    def add(self, row):
        self.session.add(row)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()


@pytest.fixture(scope="module")
def route_app():
    app = FastAPI()
    # Match production registration order, including compatibility prefixes.
    for module in (auth, commerce, operations):
        app.include_router(module.router)
    for module in (auth, commerce, operations):
        app.include_router(module.router, prefix="/api", include_in_schema=False)
    return app


@pytest.fixture
def site(monkeypatch, route_app):
    engine = create_engine("sqlite:///:memory:")
    for model in (User, Profile, Order, MODEL_BY_TABLE["coupons"], auth.AccountDeletionRequest,
                  MODEL_BY_TABLE["local_shopping_requests"], MODEL_BY_TABLE["international_orders"],
                  MODEL_BY_TABLE["order_payments"], MODEL_BY_TABLE["payments"]):
        model.__table__.create(engine)
    session = Session(engine, expire_on_commit=False)
    customer = User(id=uuid.uuid4(), email="ghaya@gmail.com", password_hash="", is_active=True)
    other = User(id=uuid.uuid4(), email="nora@gmail.com", password_hash="", is_active=True)
    admin = User(id=uuid.uuid4(), email="sara@gmail.com", password_hash="", is_active=True)
    session.add_all([customer, other, admin])
    session.commit()
    local = LocalSession(session)
    state = SimpleNamespace(account_status="active", security_version=0)
    monkeypatch.setattr(dependencies, "account_security_for", AsyncMock(return_value=state))

    async def stored_roles(_session, identity):
        return ["admin"] if identity == admin.id else ["customer"]

    monkeypatch.setattr(dependencies, "roles_for", stored_roles)
    key = uuid.uuid4().hex + uuid.uuid4().hex
    monkeypatch.setattr(tokens, "get_settings", lambda: SimpleNamespace(jwt_secret=key))
    app = route_app

    async def local_session():
        yield local

    app.dependency_overrides[get_session] = local_session

    def token(user=customer, **claims):
        payload = {"sub": str(user.id), "type": "access", "sv": 0,
                   "exp": datetime.now(timezone.utc) + timedelta(minutes=10)}
        payload.update(claims)
        return jwt.encode(payload, key, algorithm="HS256")

    yield SimpleNamespace(app=app, db=session, local=local, customer=customer, other=other,
                          admin=admin, state=state, token=token)
    app.dependency_overrides.clear()
    session.close()
    engine.dispose()


async def request(site, method, path, *, user=None, **kwargs):
    headers = {"Authorization": "Bearer " + site.token(user or site.customer)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app), base_url="http://localhost") as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_customer_coupons_scope_dates_and_response(site):
    now = datetime.now(timezone.utc)
    model = MODEL_BY_TABLE["coupons"]

    def coupon(code, scope="public", **changes):
        extra = {"scope": scope, "title": "خصم 10% على فستان", "description": "خصم على الأزياء",
                 "discount_type": "percentage", "discount_value": 10, "minimum_order_amount": 100,
                 "max_discount": 50, "usage_limit": 100, "current_uses": 5,
                 "valid_from": (now - timedelta(days=1)).isoformat(),
                 "valid_until": (now + timedelta(days=1)).isoformat()}
        extra.update(changes.pop("extra", {}))
        row = model(id=uuid.uuid4(), code=code, title="خصم فستان", amount=Decimal("10"),
                    is_active=True, extra_data=extra)
        for name, value in changes.items():
            setattr(row, name, value)
        site.db.add(row)
        return row

    public = coupon("SAVE10")
    personal = coupon("NORA10", "user", extra={"user_id": str(site.customer.id)})
    legacy_personal = coupon("FASHION10", "user", extra={"exclusive_user_id": str(site.customer.id)})
    expiry_only = coupon("AUTUMN10", expires_at=now + timedelta(days=1),
                         extra={"valid_from": None, "valid_until": None})
    coupon("DRESS15", "user", extra={"user_id": str(site.other.id)})
    coupon("FASHION20", "user", extra={"user_id": str(site.other.id), "exclusive_user_id": str(site.customer.id)})
    coupon("FASHION25", None)
    coupon("SUMMER10", "products")
    coupon("WINTER10", is_active=False)
    coupon("STYLE10", deleted_at=now)
    coupon("SAVE20", expires_at=now - timedelta(days=1))
    coupon("SAVE25", extra={"valid_from": (now + timedelta(days=2)).isoformat()})
    coupon("SAVE30", extra={"valid_until": (now - timedelta(days=2)).isoformat()})
    coupon("SAVE35", extra={"valid_until": "غير محدد"})
    site.db.commit()

    response = await request(site, "GET", "/api/coupons?user_id=" + str(site.other.id))
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    assert set(rows) == {str(public.id), str(personal.id), str(legacy_personal.id), str(expiry_only.id)}
    payload = rows[str(public.id)]
    assert set(payload) == {"id", "code", "title", "description", "discount_type", "discount_value",
                            "min_order_amount", "max_discount", "valid_from", "valid_until", "is_active",
                            "scope", "user_id", "usage_limit", "used_count"}
    assert payload["user_id"] is None
    assert payload["min_order_amount"] == 100
    assert payload["used_count"] == 5
    assert rows[str(personal.id)]["user_id"] == str(site.customer.id)
    assert rows[str(expiry_only.id)]["valid_until"].endswith("+00:00")


ORDER_GROUPS = {
    "processing": ("new", "pending", "processing", "processed"),
    "shipped": ("shipping", "shipped", "in_transit", "delivering", "delivered", "completed"),
    "review": ("delivered", "completed"),
    "returns": ("returning", "returned", "refunded"),
}


@pytest.mark.asyncio
async def test_order_counts_all_statuses_ownership_and_overlap(site):
    for group, statuses in ORDER_GROUPS.items():
        for status in statuses:
            site.db.add(Order(id=uuid.uuid4(), order_number=uuid.uuid4().hex, user_id=site.customer.id,
                              status=status, payment_status="paid", total=100))
    for status, payment in (("awaiting_payment", ""), ("awaiting_payment", "pending_payment"),
                            ("awaiting_payment", "waiting_customer_payment"), ("shipped", "unpaid")):
        site.db.add(Order(id=uuid.uuid4(), order_number=uuid.uuid4().hex, user_id=site.customer.id,
                          status=status, payment_status=payment, total=100))
    site.db.add(Order(id=uuid.uuid4(), order_number=uuid.uuid4().hex, user_id=site.other.id,
                      status="pending", payment_status="pending", total=100))
    site.db.add(Order(id=uuid.uuid4(), order_number=uuid.uuid4().hex, user_id=site.customer.id,
                      status="pending", payment_status="pending", total=100,
                      deleted_at=datetime.now(timezone.utc)))
    site.db.commit()
    response = await request(site, "GET", "/api/orders/counts?user_id=" + str(site.other.id))
    assert response.status_code == 200
    assert response.json() == {"data": {"unpaid": 4, "processing": 4, "shipped": 9, "review": 4, "returns": 3}}
    statement = site.local.statements[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "UNION ALL" in compiled
    assert "local_shopping_requests" in compiled and "international_orders" in compiled
    assert statement._limit_clause.value == 20


@pytest.mark.asyncio
async def test_order_counts_merge_sources_before_latest_twenty_limit(site):
    now = datetime.now(timezone.utc)
    for index in range(21):
        site.db.add(Order(id=uuid.uuid4(), order_number=uuid.uuid4().hex, user_id=site.customer.id,
                          status="returned", payment_status="paid", total=100,
                          created_at=now - timedelta(days=index + 1)))
    local = MODEL_BY_TABLE["local_shopping_requests"]
    international = MODEL_BY_TABLE["international_orders"]
    site.db.add(local(id=uuid.uuid4(), user_id=site.customer.id, description="فستان", status="processing",
                      amount=100, created_at=now, extra_data={}))
    site.db.add(international(id=uuid.uuid4(), user_id=site.customer.id, order_number="INTL-DRESS",
                             status="delivered", amount=100, created_at=now - timedelta(hours=1),
                             extra_data={"payment_status": "paid"}))
    # Other-account and deleted records must never displace the customer's list.
    site.db.add(local(id=uuid.uuid4(), user_id=site.other.id, status="new", created_at=now))
    site.db.add(international(id=uuid.uuid4(), user_id=site.customer.id, status="new", created_at=now,
                             deleted_at=now))
    site.db.commit()
    for user in (site.customer, site.admin):
        response = await request(site, "GET", "/api/orders/counts", user=user)
        assert response.status_code == 200
        expected = {"unpaid": 1, "processing": 1, "shipped": 1, "review": 1, "returns": 18} if user == site.customer else dict.fromkeys(("unpaid", *ORDER_GROUPS), 0)
        assert response.json() == {"data": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("source,payment,paid,expected", [
    ("local_shopping_requests", None, 0, 1),
    ("local_shopping_requests", "pending", 0, 0),
    ("local_shopping_requests", "waiting_customer_payment", 0, 1),
    ("local_shopping_requests", "payment_approved", 0, 0),
    ("local_shopping_requests", "unpaid", 50, 0),
    ("local_shopping_requests", "unpaid", 100, 0),
    ("local_shopping_requests", "partially_paid", 0, 0),
    ("international_orders", None, 0, 1),
    ("international_orders", "pending_payment", 0, 1),
    ("international_orders", "pending", 0, 0),
    ("international_orders", "paid", 0, 0),
])
async def test_order_counts_source_payment_normalization_matches_mobile(site, source, payment, paid, expected):
    model = MODEL_BY_TABLE[source]
    site.db.add(model(id=uuid.uuid4(), user_id=site.customer.id, status=" PROCESSING ", amount=100,
                      extra_data={"paymentStatus": payment, "paidAmount": paid}))
    site.db.commit()
    response = await request(site, "GET", "/api/orders/counts")
    assert response.status_code == 200
    assert response.json()["data"] == {"unpaid": expected, "processing": 1, "shipped": 0, "review": 0, "returns": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("table_name,payment_status,expected", [
    ("order_payments", "approved", 0), ("payments", "confirmed", 0),
    ("payments", "pending", 1), ("order_payments", "rejected", 1),
])
async def test_store_count_uses_the_same_confirmed_ledgers_as_order_list(site, table_name, payment_status, expected):
    order = Order(id=uuid.uuid4(), order_number=uuid.uuid4().hex, user_id=site.customer.id,
                  status="pending", payment_status="unpaid", total=100)
    site.db.add(order)
    payment = MODEL_BY_TABLE[table_name]
    site.db.add(payment(id=uuid.uuid4(), order_id=order.id, status=payment_status, amount=100))
    site.db.commit()
    response = await request(site, "GET", "/api/orders/counts")
    assert response.status_code == 200
    assert response.json()["data"]["unpaid"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["confirmed", "accepted", "preparing", "ready_for_shipment", "out_for_delivery", "rejected", "cancelled"])
async def test_order_counts_do_not_silently_use_the_older_non_mobile_status_groups(site, status):
    site.db.add(Order(id=uuid.uuid4(), order_number=uuid.uuid4().hex, user_id=site.customer.id,
                      status=status, payment_status="paid", total=100))
    site.db.commit()
    response = await request(site, "GET", "/api/orders/counts")
    assert response.status_code == 200
    assert response.json() == {"data": dict.fromkeys(("unpaid", *ORDER_GROUPS), 0)}


@pytest.mark.asyncio
async def test_order_counts_empty(site):
    response = await request(site, "GET", "/api/orders/counts")
    assert response.status_code == 200
    assert response.json() == {"data": {name: 0 for name in ("unpaid", *ORDER_GROUPS)}}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "لم أعد أستخدم الحساب"])
async def test_deletion_request_persists_pending_without_disabling_account(site, reason):
    response = await request(site, "POST", "/api/account-deletion-requests", json={"reason": reason})
    assert response.status_code == 201
    row = response.json()["data"]
    assert set(row) == {"id", "user_id", "reason", "status", "created_at"}
    assert row["user_id"] == str(site.customer.id)
    assert row["reason"] == reason
    assert row["status"] == "pending"
    assert row["created_at"]
    stored = site.db.get(auth.AccountDeletionRequest, uuid.UUID(row["id"]))
    assert stored.user_id == site.customer.id
    assert stored.reason == reason
    assert site.customer.is_active is True
    assert site.state.account_status == "active"
    assert site.state.security_version == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"reason": 123}, {"reason": []},
                                 {"reason": None, "user_id": "00000000-0000-0000-0000-000000000000"},
                                 {"reason": None, "status": "approved"}])
async def test_deletion_body_rejects_invalid_fields_without_inserting(site, body):
    response = await request(site, "POST", "/api/account-deletion-requests", json=body)
    assert response.status_code == 422
    assert site.db.query(auth.AccountDeletionRequest).count() == 0


@pytest.mark.asyncio
async def test_admin_deletion_list_preserves_ui_fields_and_enforces_roles(site):
    await request(site, "POST", "/api/account-deletion-requests", json={"reason": "لم أعد أستخدم الحساب"})
    forbidden = await request(site, "GET", "/api/admin/account-deletion-requests")
    assert forbidden.status_code == 403
    response = await request(site, "GET", "/api/admin/account-deletion-requests", user=site.admin)
    assert response.status_code == 200
    row = response.json()["data"][0]
    assert row["user_email"] == site.customer.email
    assert row["user"]["email"] == site.customer.email
    assert row["status"] == "pending"
    assert "password_hash" not in row["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("GET", "/api/coupons"), ("GET", "/api/orders/counts"),
                                        ("POST", "/api/account-deletion-requests"),
                                        ("GET", "/api/admin/account-deletion-requests")])
@pytest.mark.parametrize("credential", ["missing", "cookie", "malformed", "expired", "stale", "refresh", "tampered"])
async def test_all_endpoints_require_current_bearer_jwt(site, method, path, credential):
    headers = {}
    if credential == "cookie":
        headers["Cookie"] = "at=" + site.token()
    elif credential == "malformed":
        headers["Authorization"] = "Bearer invalid"
    elif credential == "expired":
        headers["Authorization"] = "Bearer " + site.token(exp=datetime.now(timezone.utc) - timedelta(minutes=1))
    elif credential == "stale":
        headers["Authorization"] = "Bearer " + site.token(sv=1)
    elif credential == "refresh":
        headers["Authorization"] = "Bearer " + site.token(type="refresh")
    elif credential == "tampered":
        payload = jwt.decode(site.token(), options={"verify_signature": False})
        headers["Authorization"] = "Bearer " + jwt.encode(payload, uuid.uuid4().hex, algorithm="HS256")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app), base_url="http://localhost") as client:
        response = await client.request(method, path, headers=headers, json={"reason": None} if method == "POST" else None)
    assert response.status_code == 401
    assert site.db.query(auth.AccountDeletionRequest).count() == 0


@pytest.mark.asyncio
async def test_endpoints_through_production_middleware_without_external_database(site, monkeypatch):
    from backend.app import main

    monkeypatch.setitem(main.app.dependency_overrides, get_session, site.app.dependency_overrides[get_session])
    monkeypatch.setattr(main.settings, "app_env", "test")
    limiter = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True, headers={})))
    monkeypatch.setattr(main, "DistributedRateLimitService", lambda: limiter)
    headers = {"Authorization": "Bearer " + site.token()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        for path in ("/api/coupons", "/api/orders/counts"):
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
        response = await client.post("/api/account-deletion-requests", headers=headers, json={"reason": None})
        assert response.status_code == 201
        assert response.json()["data"]["user_id"] == str(site.customer.id)
        assert response.headers["cache-control"] == "no-store"
        response = await client.get("/api/admin/account-deletion-requests",
                                    headers={"Authorization": "Bearer " + site.token(site.admin)})
        assert response.status_code == 200
        assert response.json()["data"][0]["user_email"] == site.customer.email


def test_production_route_matches_counts_before_uuid_detail():
    from backend.app.main import app
    from starlette.routing import Match

    scope = {"type": "http", "path": "/api/orders/counts", "root_path": "", "method": "GET"}
    route = next(route for route in app.routes if route.matches(scope)[0] == Match.FULL)
    assert route.endpoint is commerce.customer_order_counts
