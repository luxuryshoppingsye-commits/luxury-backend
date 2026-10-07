from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.routes import operations
from app.models import MODEL_BY_TABLE
from app.models.domain import StaffPermissionSet


class _OptionSession:
    def __init__(self):
        self.rows = {}
        self.permissions = None

    async def get(self, model, record_id, **kwargs):
        if model is StaffPermissionSet:
            return self.permissions
        return self.rows.get((model, record_id))

    def add(self, row):
        row.id = row.id or uuid.uuid4()
        row.created_at = datetime.now(timezone.utc)
        row.updated_at = row.created_at
        self.rows[(type(row), row.id)] = row

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def delete(self, row):
        del self.rows[(type(row), row.id)]


@pytest.fixture
def merchant_api(monkeypatch):
    ledger = _OptionSession()
    actor = {"id": uuid.uuid4(), "roles": {"partner"}}
    app = FastAPI()
    app.include_router(operations.router)

    async def current_user():
        if actor["id"] is None:
            raise HTTPException(status_code=401, detail="authentication_required")
        return SimpleNamespace(id=actor["id"])

    async def roles():
        return actor["roles"]

    async def session():
        return ledger

    async def rows(received_session, table, **kwargs):
        assert received_session is ledger
        model = MODEL_BY_TABLE[table]
        return [row for (row_model, _), row in ledger.rows.items() if row_model is model]

    app.dependency_overrides[operations.current_user] = current_user
    app.dependency_overrides[operations.user_roles] = roles
    app.dependency_overrides[operations.get_session] = session
    monkeypatch.setattr(operations, "_rows", rows)
    return app, ledger, actor


_OPTIONS = [
    ("brands", "زارا", "زارا للأزياء", None),
    ("colors", "أحمر", "أحمر داكن", "#B91C1C"),
    ("sizes", "M", "L", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("option,name,updated_name,code", _OPTIONS)
async def test_merchant_default_permissions_allow_own_option_crud(
    merchant_api, option, name, updated_name, code,
):
    app, ledger, actor = merchant_api
    body = {"name": name, "partner_id": str(uuid.uuid4())}
    if code:
        body["code"] = code
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        created = await client.post(f"/partner/product-options/{option}", json=body)
        assert created.status_code == 201, created.text
        payload = created.json()["data"]
        assert payload["name"] == name
        assert payload["partner_id"] == str(actor["id"])
        assert payload["can_manage"] is True
        assert payload["is_default"] is False
        assert payload["is_active"] is (option != "brands")
        if option == "brands":
            assert payload["approval_status"] == "pending"
        if code:
            assert payload["code"] == code
        record_id = payload["id"]
        updated = await client.patch(
            f"/partner/product-options/{option}/{record_id}",
            json={"name": updated_name, **({"code": "#991B1B"} if code else {})},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["name"] == updated_name
        if code:
            assert updated.json()["data"]["code"] == "#991B1B"
        listed = await client.get(f"/partner/product-options/{option}")
        assert [row["name"] for row in listed.json()["data"]] == [updated_name]
        deleted = await client.delete(f"/partner/product-options/{option}/{record_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"ok": True}
        assert not ledger.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("option,name,updated_name,code", _OPTIONS)
async def test_global_and_other_merchant_options_remain_protected(
    merchant_api, option, name, updated_name, code,
):
    app, ledger, actor = merchant_api
    model = MODEL_BY_TABLE[operations._PARTNER_OPTION_TABLES[option]]
    protected = []
    for owner in (None, uuid.uuid4()):
        row = model(name=name, is_active=True, extra_data={"partner_id": str(owner)} if owner else {})
        ledger.add(row)
        protected.append(row)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        listed = (await client.get(f"/partner/product-options/{option}")).json()["data"]
        assert len(listed) == 1
        assert listed[0]["can_manage"] is False
        assert listed[0]["is_default"] is True
        for row in protected:
            path = f"/partner/product-options/{option}/{row.id}"
            assert (await client.patch(path, json={"name": updated_name})).status_code == 404
            assert (await client.delete(path)).status_code == 404
            assert row.name == name
        assert len(ledger.rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("option,name,updated_name,code", _OPTIONS)
async def test_custom_permissions_are_not_overridden(merchant_api, option, name, updated_name, code):
    app, ledger, actor = merchant_api
    ledger.permissions = SimpleNamespace(permissions=[])
    model = MODEL_BY_TABLE[operations._PARTNER_OPTION_TABLES[option]]
    row = model(name=name, is_active=True, extra_data={"partner_id": str(actor["id"])})
    ledger.add(row)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        responses = [
            await client.post(f"/partner/product-options/{option}", json={"name": name}),
            await client.patch(f"/partner/product-options/{option}/{row.id}", json={"name": updated_name}),
            await client.delete(f"/partner/product-options/{option}/{row.id}"),
        ]
        for response in responses:
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "staff_permission_denied"
        assert len(ledger.rows) == 1
        assert row.name == name


@pytest.mark.asyncio
async def test_non_merchants_and_unauthenticated_users_cannot_manage_options(merchant_api):
    app, ledger, actor = merchant_api
    actor["roles"] = {"customer"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.post("/partner/product-options/sizes", json={"name": "M"})).status_code == 403
        actor["id"] = None
        assert (await client.post("/partner/product-options/sizes", json={"name": "M"})).status_code == 401
        assert not ledger.rows
