from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response

from backend.app.api.routes import commerce
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Order, UserCart
from backend.app.services import financial_calculator as fc


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _session(monkeypatch, *, balance=575, credited=575, active=True, percentage=5):
    loyalty = SimpleNamespace(balance=Decimal(balance))
    transactions = [
        SimpleNamespace(type="earned", amount=credited),
        SimpleNamespace(type="redeem", amount=1000),
        SimpleNamespace(type="adjustment", amount=-200),
    ]

    async def execute(statement):
        if "points_transactions" in str(statement):
            return SimpleNamespace(scalars=lambda: transactions)
        return SimpleNamespace(scalar_one_or_none=lambda: loyalty)

    monkeypatch.setattr(fc, "loyalty_program_settings", AsyncMock(return_value=fc.LoyaltyProgramSettings(is_active=active)))
    monkeypatch.setattr(fc, "loyalty_tier_catalog", AsyncMock(return_value=[
        {"name": "برونزي", "min_points": 0, "discount_percentage": 0},
        {"name": "فضي", "min_points": 500, "discount_percentage": percentage},
    ]))
    monkeypatch.setattr(fc, "_shipping_configuration", AsyncMock(return_value={}))
    added = []

    async def flush():
        for row in added:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()

    return SimpleNamespace(
        execute=AsyncMock(side_effect=execute), begin_nested=lambda: _Transaction(),
        add=added.append, flush=AsyncMock(side_effect=flush), commit=AsyncMock(),
        added=added, loyalty=loyalty,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("points,total,points_discount", [(0, "85500.00", "0.00"), (50, "80500.00", "5000.00"), (450, "40500.00", "45000.00")])
async def test_silver_discount_is_separate_from_redeemed_points(monkeypatch, points, total, points_discount):
    session = _session(monkeypatch)
    result = await fc.calculate_checkout_financials(
        session, user_id=uuid.uuid4(), subtotal=Decimal(90000),
        apply_membership_discount=True, apply_free_shipping=True,
        body={"loyaltyPointsToRedeem": points, "membershipDiscount": 1, "tierDiscountPercentage": 99, "total": 1},
    )
    assert result.membership_discount == Decimal("4500.00")
    assert result.loyalty_discount == Decimal(points_discount)
    assert result.total == Decimal(total)
    assert result.discount_total == Decimal("4500.00") + Decimal(points_discount)
    assert result.breakdown["membership"]["tier_name"] == "فضي"
    assert result.breakdown["membership_discount"] == "4500.00"
    assert {"membershipDiscount", "tierDiscountPercentage", "total"} <= set(result.breakdown["ignored_client_fields"])
    assert session.loyalty.balance == 575


@pytest.mark.asyncio
@pytest.mark.parametrize("balance,credited,active,percentage,expected", [
    (75, 575, True, 5, "4500.00"),
    (575, 575, True, 3, "2700.00"),
    (75, 75, True, 5, "0.00"),
    (575, 575, False, 5, "0.00"),
])
async def test_membership_uses_authoritative_earned_points_and_catalog(monkeypatch, balance, credited, active, percentage, expected):
    session = _session(monkeypatch, balance=balance, credited=credited, active=active, percentage=percentage)
    discount, snapshot = await fc._membership_discount(
        session, user_id=uuid.uuid4(), merchandise_total=Decimal(90000), remaining_amount=Decimal(90000),
    )
    assert discount == Decimal(expected)
    if active:
        assert snapshot["total_points"] == max(balance, credited)
    assert session.loyalty.balance == balance


@pytest.mark.asyncio
async def test_membership_rounding_and_cap_prevent_over_discount(monkeypatch):
    session = _session(monkeypatch)
    discount, _ = await fc._membership_discount(
        session, user_id=uuid.uuid4(), merchandise_total=Decimal("90000.10"), remaining_amount=Decimal(90000),
    )
    assert discount == Decimal("4500.01")
    discount, _ = await fc._membership_discount(
        session, user_id=uuid.uuid4(), merchandise_total=Decimal(90000), remaining_amount=Decimal(2000),
    )
    assert discount == Decimal("2000.00")


@pytest.mark.asyncio
async def test_manual_and_other_paths_do_not_gain_customer_membership_discount(monkeypatch):
    session = _session(monkeypatch)
    result = await fc.calculate_checkout_financials(
        session, user_id=uuid.uuid4(), subtotal=Decimal(90000), body={}, apply_free_shipping=True,
    )
    assert result.membership_discount == 0
    assert result.total == 90000
    fc.loyalty_tier_catalog.assert_not_awaited()


@pytest.mark.asyncio
async def test_coupon_points_and_membership_keep_separate_amounts(monkeypatch):
    session = _session(monkeypatch)
    monkeypatch.setattr(fc, "_coupon_discount", AsyncMock(return_value=(Decimal(3000), None, {"source": "coupon"})))
    result = await fc.calculate_checkout_financials(
        session, user_id=uuid.uuid4(), subtotal=Decimal(90000),
        body={"loyaltyPointsToRedeem": 50}, apply_membership_discount=True, apply_free_shipping=True,
    )
    assert result.coupon_discount == 3000
    assert result.membership_discount == 4500
    assert result.loyalty_discount == 5000
    assert result.total == 77500


@pytest.mark.asyncio
@pytest.mark.parametrize("points,total", [(0, "85500.00"), (50, "80500.00")])
async def test_checkout_persists_membership_in_order_and_payment_without_burning_extra_points(monkeypatch, points, total):
    session = _session(monkeypatch)
    user_id, product_id = uuid.uuid4(), uuid.uuid4()
    product = SimpleNamespace(id=product_id, name="حقيبة جلد", image_url=None, partner_id=None, track_inventory=False)
    item = UserCart(id=uuid.uuid4(), product_id=product_id, user_id=user_id, quantity=1)
    monkeypatch.setattr(commerce, "validate_payment_method_for_checkout", AsyncMock(return_value="cash_on_delivery"))
    monkeypatch.setattr(commerce, "validate_customer_checkout_address", lambda address: address)
    monkeypatch.setattr(commerce, "_validated_cart_lines", AsyncMock(return_value=([(item, product, None, Decimal(90000))], Decimal(90000), Decimal(0))))
    monkeypatch.setattr(commerce, "_create_notification", AsyncMock())
    monkeypatch.setattr(commerce, "_return_policy_snapshot", lambda *args: {})
    request = SimpleNamespace(json=AsyncMock(return_value={
        "paymentMethod": "cash_on_delivery", "shippingAddress": {"city": "صنعاء", "address": "حدة - شارع الزبيري"},
        "loyaltyPointsToRedeem": points,
    }))
    dto = await commerce.checkout(request, Response(), None, SimpleNamespace(id=user_id), session)
    order = next(row for row in session.added if isinstance(row, Order))
    assert order.total == Decimal(total)
    assert order.discount_total == Decimal(4500 + points * 100)
    assert order.extra_data["financial_breakdown"]["membership_discount"] == "4500.00"
    assert Decimal(str(dto["total"])) == Decimal(total)
    assert dto["financial_breakdown"]["membership"]["tier_name"] == "فضي"
    payments = [row for row in session.added if isinstance(row, MODEL_BY_TABLE["order_payments"])]
    assert payments[0].amount == Decimal(total)
    transactions = [row for row in session.added if isinstance(row, MODEL_BY_TABLE["points_transactions"])]
    assert session.loyalty.balance == 575 - points
    assert len(transactions) == (1 if points else 0)
    if points:
        assert transactions[0].amount == points
        assert transactions[0].extra_data["discount_yer"] == "5000.00"
    session.commit.assert_awaited_once()
