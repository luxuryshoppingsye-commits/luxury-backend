from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import report_admin_services as ras


@pytest.mark.asyncio
async def test_order_activity_summary_includes_pending_and_supplemental_orders(monkeypatch) -> None:
    pending_order = SimpleNamespace(total="125.00", currency_code="YER")
    local_request = {"amount": "75.00"}

    monkeypatch.setattr(
        ras.RevenueRecognitionService,
        "eligible_orders",
        AsyncMock(return_value=[pending_order]),
    )
    monkeypatch.setattr(
        ras.RevenueRecognitionService,
        "_supplemental_orders",
        AsyncMock(return_value=[("local_shopping_requests", local_request)]),
    )
    monkeypatch.setattr(ras, "serialize_record", lambda record: record)

    result = await ras.RevenueRecognitionService.order_activity_summary(object())

    assert result == {
        "order_count": 2,
        "order_value": ras.money("200.00"),
        "currency_code": "YER",
    }
