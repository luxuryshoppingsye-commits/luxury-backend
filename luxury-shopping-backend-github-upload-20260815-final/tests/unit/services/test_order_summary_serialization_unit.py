from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.api.routes import commerce


@pytest.mark.asyncio
async def test_manual_order_keeps_customer_name_when_read_as_a_single_order():
    order = SimpleNamespace(user_id=uuid.uuid4(), extra_data={"customer": {"full_name": "أحمد محمد"}})
    session = SimpleNamespace(execute=AsyncMock())
    payload = {"shipping_address": {"full_name": "أحمد محمد", "phone": "777000202"}}
    await commerce._attach_current_customer_name(session, order, payload)
    assert payload["customer_name"] == "أحمد محمد"
    assert payload["shipping_address"] == {"full_name": "أحمد محمد", "phone": "777000202"}
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_preserves_manual_customer_name_and_regular_profile_name():
    staff_id, customer_id = uuid.uuid4(), uuid.uuid4()
    orders = [
        SimpleNamespace(user_id=staff_id, extra_data={"customer": {"full_name": "أحمد محمد"}}),
        SimpleNamespace(user_id=customer_id, extra_data={}),
    ]
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [
        (staff_id, "إدارة متجر رفاهية التسوق"), (customer_id, "سارة"),
    ])))
    payloads = [
        {"shipping_address": {"full_name": "أحمد محمد", "city": "صنعاء"}},
        {"shipping_address": {"full_name": "الاسم السابق", "city": "عدن"}},
    ]
    await commerce._attach_current_customer_names(session, orders, payloads)
    assert payloads[0]["customer_name"] == "أحمد محمد"
    assert payloads[0]["shipping_address"] == {"full_name": "أحمد محمد", "city": "صنعاء"}
    assert payloads[1]["customer_name"] == "سارة"
    assert payloads[1]["shipping_address"] == {"full_name": "سارة", "city": "عدن"}


@pytest.mark.asyncio
async def test_order_summary_serializer_exposes_paid_and_remaining_amounts(monkeypatch: pytest.MonkeyPatch) -> None:
    order = SimpleNamespace(id="order-1", total=Decimal("60000.00"), extra_data={})
    monkeypatch.setattr(commerce, "_serialize_order", lambda _: {
        "id": "order-1",
        "total": "60000.00",
        "shipping_total": "0.00",
    })
    monkeypatch.setattr(commerce, "_payment_receipts_by_order", AsyncMock(return_value={}))
    monkeypatch.setattr(commerce, "_attach_courier_assignments", AsyncMock())

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
    monkeypatch.setattr(commerce, "_payment_receipts_by_order", AsyncMock(return_value={}))
    monkeypatch.setattr(commerce, "_attach_courier_assignments", AsyncMock())

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
    monkeypatch.setattr(commerce, "_payment_receipts_by_order", AsyncMock(return_value={}))
    monkeypatch.setattr(commerce, "_attach_courier_assignments", AsyncMock())
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


@pytest.mark.asyncio
async def test_order_summary_serializer_prefers_current_profile_name_over_shipping_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_id = uuid.uuid4()
    user_id = uuid.uuid4()
    order = SimpleNamespace(
        id=order_id,
        user_id=user_id,
        total=Decimal("1000.00"),
        extra_data={},
    )
    monkeypatch.setattr(commerce, "_serialize_order", lambda _: {
        "id": str(order_id),
        "total": "1000.00",
        "shipping_total": "0.00",
        "shipping_address": {"full_name": "الاسم القديم", "city": "صنعاء"},
    })
    monkeypatch.setattr(commerce, "_payment_receipts_by_order", AsyncMock(return_value={}))
    monkeypatch.setattr(commerce, "_attach_courier_assignments", AsyncMock())

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return []

        def all(self):
            return self._rows

    session = SimpleNamespace(execute=AsyncMock(side_effect=[
        Result([(user_id, "الاسم الجديد")]),
        Result([]),
        Result([]),
        Result([]),
        Result([]),
    ]))

    rows = await commerce._serialize_orders_with_financials(session, [order])

    assert rows[0]["customer_name"] == "الاسم الجديد"
    assert rows[0]["shipping_address"]["full_name"] == "الاسم الجديد"
    assert rows[0]["shipping_address"]["city"] == "صنعاء"


def test_receipt_reference_is_exposed_on_order_and_payment_payloads() -> None:
    receipt_id = uuid.uuid4()
    receipt = SimpleNamespace(
        id=receipt_id,
        status="pending_review",
    )
    order_payload = {"id": str(uuid.uuid4())}
    payment_payloads = [{"id": str(uuid.uuid4()), "receipt_url": None}]

    commerce._attach_latest_receipt_to_order_payload(order_payload, [receipt])
    commerce._attach_receipts_to_payment_payloads(payment_payloads, [receipt])

    expected_ref = f"receipt:{receipt_id}"
    assert order_payload["receipt_url"] == expected_ref
    assert order_payload["receiptPath"] == expected_ref
    assert payment_payloads[0]["receipt_url"] == expected_ref
    assert payment_payloads[0]["payment_receipt_id"] == str(receipt_id)
