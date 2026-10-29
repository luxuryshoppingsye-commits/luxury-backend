import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from backend.app.api.routes import commerce
from backend.app.api.routes.commerce import _order_status_notification_label
from backend.app.services.notification_service import customer_notification_allowed


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("accepted", "تم اعتماد الطلب"),
        ("processing", "قيد التجهيز"),
        ("shipped", "تم شحن الطلب"),
        ("delivered", "تم تسليم الطلب"),
        ("cancelled", "تم إلغاء الطلب"),
    ],
)
def test_order_status_notification_label_is_arabic(status, expected):
    assert _order_status_notification_label(status) == expected


def test_unknown_status_keeps_the_original_value_for_diagnostics():
    assert _order_status_notification_label("custom_status") == "custom_status"


@pytest.mark.asyncio
async def test_rollback_notifies_customer_and_repeat_status_uses_a_new_event(monkeypatch):
    order = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), order_number="ORD-261012-0001", status="pending")
    actor = SimpleNamespace(id=uuid.uuid4())
    saved = []
    session = SimpleNamespace(
        execute=AsyncMock(), add=Mock(side_effect=saved.append), commit=AsyncMock(),
    )
    notify = AsyncMock()
    monkeypatch.setattr(commerce, "require_staff_permission", AsyncMock())
    monkeypatch.setattr(commerce, "award_loyalty_points_for_fulfilled_order", AsyncMock())
    monkeypatch.setattr(commerce, "add_audit_log", Mock())
    monkeypatch.setattr(commerce, "serialize_record", lambda row: {"status": row.status})
    monkeypatch.setattr(commerce, "_create_notification", notify)
    request = SimpleNamespace(json=AsyncMock(return_value={"status": "accepted"}))
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: order)
    await commerce.change_order_status(order.id, request, actor, {"admin"}, session)
    first_key = notify.await_args.kwargs["deduplication_key"]
    history = saved[-1]
    session.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda: order),
        SimpleNamespace(scalar_one_or_none=lambda: history),
    ]
    await commerce.rollback_order_status(order.id, actor, {"admin"}, session)
    rollback = notify.await_args.kwargs
    assert order.status == "pending"
    assert rollback["user_id"] == order.user_id
    assert rollback["order_id"] == order.id
    assert rollback["type"] == "order_status_rolled_back"
    assert customer_notification_allowed(rollback["type"])
    assert "تم التراجع" in rollback["body"]
    assert rollback["payload"]["order_status"] == "pending"
    assert rollback["payload"]["deep_link"] == f"/orders/{order.id}"
    assert rollback["payload"]["title_en"] == "Order status reverted"
    session.execute.side_effect = None
    await commerce.change_order_status(order.id, request, actor, {"admin"}, session)
    assert notify.await_args.kwargs["deduplication_key"] != first_key
    assert notify.await_count == 3
    assert session.commit.await_count == 3
