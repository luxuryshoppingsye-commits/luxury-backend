import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionFactory
from app.main import app
from app.models import MODEL_BY_TABLE
from app.models.domain import Order, Profile, User, UserRole
from app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def courier_records():
    settings = get_settings()
    assert settings.is_test_environment and "127.0.0.1" in settings.database_url
    identifiers = {name: uuid.uuid4() for name in ("admin", "customer", "ahmed", "omar", "courier", "local", "order")}
    emails = {}
    async with SessionFactory() as session:
        for name, role, full_name in (("admin", "admin", "غاية"), ("customer", "customer", "سارة"), ("ahmed", "courier", "أحمد"), ("omar", "delivery", "عمر")):
            emails[name] = f"{name}{identifiers[name].hex[:8]}@gmail.com"
            session.add(User(id=identifiers[name], email=emails[name], password_hash=hash_password("Admin1234"), is_active=True))
            await session.flush()
            session.add(Profile(user_id=identifiers[name], full_name=full_name, email=emails[name], phone="777123456", city="صنعاء"))
            session.add(UserRole(user_id=identifiers[name], role=role))
        session.add(MODEL_BY_TABLE["couriers"](id=identifiers["courier"], name="أحمد", phone="777123456", status="active"))
        session.add(MODEL_BY_TABLE["local_shopping_requests"](id=identifiers["local"], user_id=identifiers["customer"], description="فستان أسود", amount=2000, status="pending", extra_data={"customer_note": "التوصيل إلى صنعاء"}))
        session.add(Order(id=identifiers["order"], user_id=identifiers["customer"], order_number=f"LS-DRESS-{identifiers['order'].hex[:8]}", total=2000, subtotal=2000, status="confirmed", currency_code="YER"))
        await session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8106") as client:
        tokens = {}
        for name, email in emails.items():
            response = await client.post("/api/auth/login", json={"email": email, "password": "Admin1234"})
            assert response.status_code == 200, response.text
            tokens[name] = {"Authorization": "Bearer " + response.json()["session"]["access_token"]}
        client.headers.update(tokens["admin"])
        yield identifiers, emails, tokens, client


def staff_payload(role):
    return {"email": f"ahmed{uuid.uuid4().hex[:8]}@gmail.com", "password": "Admin1234", "full_name": "أحمد علي", "phone": "777123456", "job_title": "مندوب توصيل", "department": "التوصيل", "role": role}


@pytest.mark.parametrize("role", ["courier", "delivery", "employee", "finance", "logistics"])
async def test_administrator_can_create_accounts_without_changing_role_permissions(courier_records, role):
    _, _, _, client = courier_records
    response = await client.post("/api/admin/staff/members", json=staff_payload(role))
    assert response.status_code == 201, response.text
    user_id = response.json()["data"]["user_id"]
    async with SessionFactory() as session:
        roles = (await session.execute(select(UserRole.role).where(UserRole.user_id == uuid.UUID(user_id)))).scalars().all()
        assert roles == [role]
    listing = await client.get("/api/admin/staff/roles")
    assert any(row["user_id"] == user_id and row["role"] == role for row in listing.json()["data"])
    if role in {"courier", "delivery"}:
        options = await client.get("/api/admin/courier-users/options")
        assert any(row["id"] == user_id for row in options.json()["data"])
        responsible_staff = await client.get("/api/admin/staff/members")
        assert not any(row["id"] == user_id for row in responsible_staff.json()["data"])


async def test_active_courier_without_account_can_receive_local_assignment(courier_records):
    ids, _, tokens, client = courier_records
    response = await client.patch(f"/api/admin/local-requests/{ids['local']}/courier-assignment", json={"courier_id": str(ids["courier"])})
    assert response.status_code == 200, response.text
    assignment = response.json()["data"]
    assert assignment["courier_id"] == str(ids["courier"]) and assignment["user_id"] is None
    current = await client.get(f"/api/admin/local-requests/{ids['local']}/courier-assignment")
    assert current.json()["data"]["id"] == assignment["id"]
    async with SessionFactory() as session:
        local = await session.get(MODEL_BY_TABLE["local_shopping_requests"], ids["local"])
        assert local.extra_data["customer_note"] == "التوصيل إلى صنعاء"
        assert local.extra_data["courier_assignment_id"] == assignment["id"]
    for name in ("ahmed", "omar"):
        visible = await client.get("/delivery/assignments", headers=tokens[name])
        assert visible.status_code == 200
        assert not any(row["id"] == assignment["id"] for row in visible.json())


async def test_linking_account_exposes_existing_local_and_store_assignments_only_to_owner(courier_records):
    ids, _, tokens, client = courier_records
    local = await client.patch(f"/api/admin/local-requests/{ids['local']}/courier-assignment", json={"courier_id": str(ids["courier"])})
    store = await client.patch(f"/api/admin/orders/{ids['order']}/courier-assignment", json={"courier_id": str(ids["courier"])})
    assert local.status_code == store.status_code == 200
    assignment_ids = {local.json()["data"]["id"], store.json()["data"]["id"]}
    linked = await client.patch(f"/api/admin/couriers/{ids['courier']}/account", json={"user_id": str(ids["ahmed"])})
    assert linked.status_code == 200 and linked.json()["account_linked"] is True
    own = await client.get("/delivery/assignments", headers=tokens["ahmed"])
    assert assignment_ids <= {row["id"] for row in own.json()}
    assert {row["order_type"] for row in own.json() if row["id"] in assignment_ids} == {"local", "order"}
    other = await client.get("/delivery/assignments", headers=tokens["omar"])
    assert not assignment_ids & {row["id"] for row in other.json()}
    local_id = local.json()["data"]["id"]
    forbidden = await client.patch(f"/delivery/assignments/{local_id}/status", headers=tokens["omar"], json={"status": "accepted"})
    assert forbidden.status_code == 404
    for status in ("accepted", "picked_up", "out_for_delivery", "delivered"):
        changed = await client.patch(f"/delivery/assignments/{local_id}/status", headers=tokens["ahmed"], json={"status": status})
        assert changed.status_code == 200, changed.text
    async with SessionFactory() as session:
        assert (await session.get(MODEL_BY_TABLE["local_shopping_requests"], ids["local"])).status == "delivered"
        assert (await session.get(Order, ids["order"])).status == "confirmed"


@pytest.mark.parametrize("name", ["customer", "ahmed", "omar"])
async def test_customer_and_courier_cannot_manage_accounts_assignments_or_finance(courier_records, name):
    ids, _, tokens, client = courier_records
    for endpoint in ("/api/admin/staff/roles", "/api/admin/courier-users/options", "/api/admin/couriers", f"/api/admin/orders/{ids['order']}/courier-assignment"):
        response = await client.get(endpoint, headers=tokens[name])
        assert response.status_code == 403, (endpoint, response.text)
    response = await client.post("/api/admin/staff/members", headers=tokens[name], json=staff_payload("courier"))
    assert response.status_code == 403
    response = await client.patch(f"/api/admin/local-requests/{ids['local']}/courier-assignment", headers=tokens[name], json={"courier_id": str(ids["courier"])})
    assert response.status_code == 403
    response = await client.post(f"/api/payments/orders/{ids['order']}", headers=tokens[name], json={"amount": 10, "payment_method": "cash", "status": "confirmed"})
    assert response.status_code == 403


@pytest.mark.parametrize("status", ["delivered", "cancelled", "rejected"])
async def test_closed_local_requests_keep_assignment_guard(courier_records, status):
    ids, _, _, client = courier_records
    async with SessionFactory() as session:
        local = await session.get(MODEL_BY_TABLE["local_shopping_requests"], ids["local"])
        local.status = status
        await session.commit()
    response = await client.patch(f"/api/admin/local-requests/{ids['local']}/courier-assignment", json={"courier_id": str(ids["courier"])})
    assert response.status_code == 409 and response.json()["detail"] == "local_request_delivery_closed"


@pytest.mark.parametrize("inactive", ["status", "flag"])
async def test_inactive_courier_cannot_receive_local_assignment(courier_records, inactive):
    ids, _, _, client = courier_records
    async with SessionFactory() as session:
        courier = await session.get(MODEL_BY_TABLE["couriers"], ids["courier"])
        if inactive == "status":
            courier.status = "inactive"
        else:
            courier.extra_data = {"is_active": False}
        await session.commit()
    response = await client.patch(f"/api/admin/local-requests/{ids['local']}/courier-assignment", json={"courier_id": str(ids["courier"])})
    assert response.status_code == 422 and response.json()["detail"] == "active_courier_required"


async def test_account_link_rejects_customer_and_duplicate_courier(courier_records):
    ids, _, _, client = courier_records
    response = await client.patch(f"/api/admin/couriers/{ids['courier']}/account", json={"user_id": str(ids["customer"])})
    assert response.status_code == 409 and response.json()["detail"] == "courier_account_role_required"
    response = await client.patch(f"/api/admin/couriers/{ids['courier']}/account", json={"user_id": str(ids["ahmed"])})
    assert response.status_code == 200
    second = await client.post("/api/admin/couriers", json={"name": "عمر", "phone": "777654321", "status": "active"})
    assert second.status_code == 201, second.text
    response = await client.patch(f"/api/admin/couriers/{second.json()['data']['id']}/account", json={"user_id": str(ids["ahmed"])})
    assert response.status_code == 409 and response.json()["detail"] == "courier_account_already_linked"


async def test_reassign_unassign_and_retry_preserve_assignment_history(courier_records):
    ids, _, _, client = courier_records
    path = f"/api/admin/local-requests/{ids['local']}/courier-assignment"
    assigned = await client.patch(path, json={"courier_id": str(ids["courier"])})
    repeated = await client.patch(path, json={"courier_id": str(ids["courier"])})
    assert assigned.json()["data"]["id"] == repeated.json()["data"]["id"]
    second = await client.post("/api/admin/couriers", json={"name": "عمر", "phone": "777654321", "status": "active"})
    replacement = await client.patch(path, json={"courier_id": second.json()["data"]["id"]})
    assert replacement.status_code == 200
    removed = await client.patch(path, json={"courier_id": None})
    assert removed.status_code == 200 and removed.json()["data"] is None
    async with SessionFactory() as session:
        model = MODEL_BY_TABLE["courier_assignments"]
        history = (await session.execute(select(model).where(model.extra_data["local_request_id"].astext == str(ids["local"])))).scalars().all()
        assert len(history) == 2
        assert all(row.status == "cancelled" and row.extra_data["is_current"] is False for row in history)


async def test_existing_account_can_be_given_courier_role_and_listed(courier_records):
    ids, _, _, client = courier_records
    added = await client.post("/api/admin/staff/roles", json={"user_id": str(ids["customer"]), "role": "courier"})
    assert added.status_code == 201
    options = await client.get("/api/admin/courier-users/options")
    assert any(row["id"] == str(ids["customer"]) for row in options.json()["data"])
    updated = await client.patch(f"/api/admin/staff/roles/{ids['customer']}:courier", json={"role": "delivery"})
    assert updated.status_code == 200 and updated.json()["data"]["role"] == "delivery"
