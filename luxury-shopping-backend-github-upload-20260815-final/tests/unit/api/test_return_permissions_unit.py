from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routes.commerce import list_admin_returns, update_return_status


def permission_session(permissions):
    return SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(permissions=permissions)),
        execute=AsyncMock(return_value=Mock()),
        commit=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "permissions", "status", "required"),
    [
        ("finance", ["finance.view"], "refund_pending", "finance.update"),
        ("finance", ["accounting.view"], "refunded", "finance.update"),
        ("logistics", ["orders.update", "orders.view"], "refund_pending", "finance.update"),
        ("logistics", ["orders.view"], "inspection_passed", "orders.update"),
        ("staff", ["tickets.view", "tickets.update"], "approved", "orders.update"),
    ],
)
async def test_actor_cannot_change_return_without_update_permission(role, permissions, status, required):
    session = permission_session(permissions)
    request = SimpleNamespace(json=AsyncMock(return_value={"status": status}))
    with pytest.raises(HTTPException) as exc:
        await update_return_status(uuid4(), request, SimpleNamespace(id=uuid4()), {role}, session)
    assert exc.value.status_code == 403
    assert exc.value.detail["permission"] == required
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "permissions", "status"),
    [
        ("finance", ["finance.update"], "refund_pending"),
        ("logistics", ["orders.update"], "inspection_passed"),
        ("admin", [], "refunded"),
    ],
)
async def test_authorized_actor_can_reach_return_lookup(role, permissions, status):
    session = permission_session(permissions)
    session.execute.return_value.scalar_one_or_none.return_value = None
    request = SimpleNamespace(json=AsyncMock(return_value={"status": status}))
    with pytest.raises(HTTPException) as exc:
        await update_return_status(uuid4(), request, SimpleNamespace(id=uuid4()), {role}, session)
    assert exc.value.status_code == 404
    session.execute.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions", [["finance.view"], ["accounting.view"], ["orders.view"]])
async def test_authorized_staff_read_returns(permissions):
    session = permission_session(permissions)
    session.execute.return_value.scalars.return_value = []
    result = await list_admin_returns(None, SimpleNamespace(id=uuid4()), {"finance"}, session)
    assert result == {"data": []}
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_staff_without_return_view_does_not_read_return_rows():
    session = permission_session(["dashboard.view"])
    with pytest.raises(HTTPException) as exc:
        await list_admin_returns(None, SimpleNamespace(id=uuid4()), {"finance"}, session)
    assert exc.value.status_code == 403
    session.execute.assert_not_awaited()
