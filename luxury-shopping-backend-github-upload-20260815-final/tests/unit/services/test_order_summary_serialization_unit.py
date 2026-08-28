from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.api.routes import commerce


@pytest.mark.asyncio
async def test_order_summary_serializer_exposes_paid_and_remaining_amounts(monkeypatch: pytest.MonkeyPatch) -> None:
    order = SimpleNamespace(id="order-1", total=Decimal("60000.00"), extra_data={})
    monkeypatch.setattr(commerce, "_serialize_order", lambda _: {
        "id": "order-1",
        "total": "60000.00",
        "shipping_total": "0.00",
    })

    class Result:
        def scalars(self):
            return []

        def all(self):
            return []

    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    rows = await commerce._serialize_orders_with_financials(session, [order])

    assert rows == [{
        "id": "order-1",
        "total": "60000.00",
        "shipping_total": "0.00",
        "items": [],
        "paid_amount": "0.00",
        "remaining_balance": "60000.00",
        "shipping_cost": "0.00",
    }]


@pytest.mark.asyncio
async def test_order_summary_serializer_aggregates_confirmed_ledger_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    order_id = uuid.uuid4()
    order = SimpleNamespace(id=order_id, total=Decimal("60000.00"), extra_data={})
    monkeypatch.setattr(commerce, "_serialize_order", lambda _: {
        "id": str(order_id),
        "total": "60000.00",
        "payment_status": "pending",
        "shipping_total": "0.00",
    })

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self._rows

        def all(self):
            return self._rows

    session = SimpleNamespace(execute=AsyncMock(side_effect=[
        Result([]),
        Result([(order_id, Decimal("10000.00"))]),
        Result([(order_id, Decimal("5000.00"))]),
    ]))
    rows = await commerce._serialize_orders_with_financials(session, [order])

    assert rows[0]["paid_amount"] == "15000.00"
    assert rows[0]["remaining_balance"] == "45000.00"
    assert rows[0]["payment_status"] == "partial"


@pytest.mark.asyncio
async def test_order_summary_serializer_includes_order_items_for_admin_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    order_id = uuid.uuid4()
    order = SimpleNamespace(id=order_id, total=Decimal("1000.00"), extra_data={})
    item = SimpleNamespace(order_id=order_id, product_name="منتج محفوظ", quantity=2)
    monkeypatch.setattr(commerce, "_serialize_order", lambda _: {
        "id": str(order_id),
        "total": "1000.00",
        "shipping_total": "0.00",
    })
    monkeypatch.setattr(commerce, "serialize_record", lambda row: {
        "order_id": str(row.order_id),
        "product_name": row.product_name,
        "quantity": row.quantity,
    })

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self._rows

        def all(self):
            return self._rows

    session = SimpleNamespace(execute=AsyncMock(side_effect=[
        Result([item]),
        Result([]),
        Result([]),
    ]))
    rows = await commerce._serialize_orders_with_financials(session, [order])

    assert rows[0]["items"] == [{
        "order_id": str(order_id),
        "product_name": "منتج محفوظ",
        "quantity": 2,
    }]
