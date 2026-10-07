from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from fastapi import HTTPException

from backend.app.api.routes import operations


@pytest.mark.asyncio
async def test_reconciliation_converts_each_source_currency_using_stored_rates(monkeypatch) -> None:
    currencies = [
        {"code": "YER", "exchange_rate": 1, "is_active": True},
        {"code": "USD", "exchange_rate": 1 / 535, "is_active": True, "is_default": True, "symbol": "$"},
    ]
    monkeypatch.setattr(operations, "_rows", AsyncMock(return_value=currencies))
    monkeypatch.setattr(operations, "serialize_record", lambda row: row)
    monkeypatch.setattr(
        operations.RevenueRecognitionService,
        "eligible_orders",
        AsyncMock(return_value=[SimpleNamespace(total=Decimal("140"), currency_code="YER")]),
    )
    monkeypatch.setattr(operations.RevenueRecognitionService, "_supplemental_orders", AsyncMock(return_value=[]))
    recognized = [SimpleNamespace(payment_total=Decimal("70"), refund_total=Decimal("0"), net_revenue=Decimal("70"), currency_code="YER")]
    monkeypatch.setattr(operations.RevenueRecognitionService, "order_rows", AsyncMock(return_value=recognized))
    session = MagicMock()
    empty_result = MagicMock()
    empty_result.all.return_value = []
    session.execute = AsyncMock(return_value=empty_result)

    result = await operations.api_dashboard_financial_reconciliation(staff=SimpleNamespace(), session=session)

    assert result["data"]["currencyCode"] == "USD"
    assert result["data"]["totalRevenue"] == 0.26
    assert result["data"]["totalCollected"] == 0.13
    assert result["data"]["totalOutstanding"] == 0.13
    assert result["data"]["collectionRate"] == 50.0


@pytest.mark.asyncio
async def test_reconciliation_refuses_missing_default_exchange_rate(monkeypatch) -> None:
    monkeypatch.setattr(
        operations,
        "_rows",
        AsyncMock(return_value=[{"code": "USD", "is_active": True, "is_default": True}]),
    )
    monkeypatch.setattr(operations, "serialize_record", lambda row: row)

    with pytest.raises(HTTPException) as error:
        await operations.api_dashboard_financial_reconciliation(staff=SimpleNamespace(), session=MagicMock())

    assert error.value.status_code == 503
    assert error.value.detail == "admin_exchange_rate_missing:USD"


@pytest.mark.asyncio
async def test_finance_expense_sum_converts_grouped_currencies() -> None:
    result = MagicMock()
    result.all.return_value = [("YER", Decimal("2000")), ("USD", Decimal("1"))]
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    total = await operations._sum_amount_converted(
        session, "general_expenses",
        {"YER": Decimal("1"), "USD": Decimal("1") / Decimal("535")}, "YER",
    )

    assert total == Decimal("2535.00")
    assert "GROUP BY" in str(session.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_live_kpis_use_saved_currency_and_no_invented_monthly_target(monkeypatch) -> None:
    monkeypatch.setattr(
        operations,
        "_admin_currency_context",
        AsyncMock(return_value=({"YER": Decimal("1"), "USD": Decimal("1") / Decimal("535")}, {"code": "USD", "symbol": "$"})),
    )
    paid_order = SimpleNamespace(net_revenue=Decimal("535"), currency_code="YER")
    monkeypatch.setattr(
        operations.RevenueRecognitionService,
        "order_rows",
        AsyncMock(side_effect=[[paid_order], [], [paid_order], [paid_order]]),
    )
    monkeypatch.setattr(operations, "_count", AsyncMock(return_value=1))
    monkeypatch.setattr(operations, "_admin_order_period_counts", AsyncMock(return_value=(3, 5, 7)))
    monkeypatch.setattr(operations, "_customer_count", AsyncMock(return_value=2))
    monkeypatch.setattr(operations, "_dashboard_monthly_target_yer", AsyncMock(return_value=None))

    result = await operations.api_dashboard_live_kpis(staff=SimpleNamespace(), session=MagicMock())

    assert result["data"]["currencyCode"] == "USD"
    assert result["data"]["todayRevenue"] == 1.0
    assert result["data"]["todayOrders"] == 3
    assert result["data"]["todayPaidOrders"] == 1
    assert result["data"]["weekPaidOrders"] == 1
    assert result["data"]["monthPaidOrders"] == 1
    assert result["data"]["avgOrderValue"] == 1.0
    assert result["data"]["targetAmount"] is None
    assert result["data"]["targetProgress"] is None


def test_live_kpi_periods_match_local_order_dates_across_month_boundary() -> None:
    local_now = datetime(2026, 10, 1, 0, 30, tzinfo=timezone(timedelta(hours=3)))

    today, yesterday, week, month = operations._admin_kpi_period_bounds(local_now)

    assert today == datetime(2026, 9, 30, 21, tzinfo=timezone.utc)
    assert yesterday == datetime(2026, 9, 29, 21, tzinfo=timezone.utc)
    assert month == today
    assert week == local_now.astimezone(timezone.utc) - timedelta(days=7)


@pytest.mark.asyncio
async def test_order_counts_include_store_local_and_international_without_list_limits() -> None:
    session = MagicMock()
    results = []
    for counts in ((1, 2, 3), (4, 5, 6), (7, 8, 9)):
        result = MagicMock()
        result.one.return_value = counts
        results.append(result)
    session.execute = AsyncMock(side_effect=results)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)

    totals = await operations._admin_order_period_counts(
        session, today=start, week=start, month=start
    )

    assert totals == (12, 15, 18)
    assert session.execute.await_count == 3


@pytest.mark.asyncio
async def test_customer_stats_count_full_database_and_convert_collected_revenue(monkeypatch) -> None:
    values = []
    for amount in (10, 2, 3, 4):
        query_result = MagicMock()
        query_result.scalar_one.return_value = amount
        values.append(query_result)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=values)
    monkeypatch.setattr(
        operations,
        "_admin_currency_context",
        AsyncMock(return_value=({"YER": Decimal("1"), "USD": Decimal("1") / Decimal("535")}, {"code": "USD", "symbol": "$"})),
    )
    monkeypatch.setattr(
        operations.RevenueRecognitionService,
        "order_rows",
        AsyncMock(return_value=[SimpleNamespace(net_revenue=Decimal("535"), currency_code="YER")]),
    )

    result = await operations.api_admin_customer_stats(
        staff=SimpleNamespace(), roles={"admin"}, session=session
    )

    assert result["data"]["total"] == 10
    assert result["data"]["newCustomers"] == 2
    assert result["data"]["vip"] == 3
    assert result["data"]["withOrders"] == 4
    assert result["data"]["collectedRevenue"] == 1.0
    assert result["data"]["currencyCode"] == "USD"


@pytest.mark.asyncio
async def test_customer_list_replaces_unpaid_mixed_order_totals(monkeypatch) -> None:
    customer_id, order_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(
        operations.AdminCustomerAccessService,
        "list_customers",
        AsyncMock(return_value=[{"user_id": str(customer_id), "total_spent": "9000"}]),
    )
    monkeypatch.setattr(
        operations,
        "_admin_currency_context",
        AsyncMock(return_value=({"YER": Decimal("1"), "USD": Decimal("1") / Decimal("535")}, {"code": "USD", "symbol": "$"})),
    )
    monkeypatch.setattr(
        operations.RevenueRecognitionService,
        "_regular_order_rows",
        AsyncMock(return_value=[SimpleNamespace(order_id=order_id, net_revenue=Decimal("535"), currency_code="YER")]),
    )
    query_result = MagicMock()
    query_result.all.return_value = [(order_id, customer_id)]
    session = MagicMock()
    session.execute = AsyncMock(return_value=query_result)

    result = await operations.api_admin_customers_alias(
        staff=SimpleNamespace(), roles={"admin"}, session=session
    )

    assert result["data"][0]["total_spent"] == "1.00"
    assert result["data"][0]["currency_code"] == "USD"


@pytest.mark.asyncio
async def test_monthly_target_rejects_nonpositive_amount_before_writing() -> None:
    request = MagicMock()
    request.json = AsyncMock(return_value={"amount": 0})
    session = MagicMock()

    with pytest.raises(HTTPException) as error:
        await operations.api_update_admin_dashboard_target(
            request=request, admin=SimpleNamespace(), session=session
        )

    assert error.value.status_code == 422
    session.add.assert_not_called()
    session.commit.assert_not_called()
