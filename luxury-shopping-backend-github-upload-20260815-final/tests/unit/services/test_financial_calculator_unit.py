from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.models.domain import Product, ProductVariant
from backend.app.services import financial_calculator as fc


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", Decimal("0.00")),
        ("0.004", Decimal("0.00")),
        ("0.005", Decimal("0.01")),
        ("1.005", Decimal("1.01")),
        ("2.675", Decimal("2.68")),
        (Decimal("99.995"), Decimal("100.00")),
        (None, Decimal("0.00")),
    ],
)
def test_money_uses_decimal_half_up_rounding(raw: object, expected: Decimal) -> None:
    assert fc.money(raw) == expected


@pytest.mark.parametrize("raw", ["not-money", object(), "-0.01"])
def test_money_rejects_invalid_or_negative_values(raw: object) -> None:
    with pytest.raises(HTTPException) as exc_info:
        fc.money(raw)

    assert exc_info.value.status_code == 400


def test_money_or_zero_safely_handles_invalid_values() -> None:
    assert fc.money_or_zero("not-money") == Decimal("0.00")
    assert fc.money_or_zero("-10") == Decimal("0.00")


def test_request_hash_is_canonical_and_ignores_idempotency_key() -> None:
    first = fc.request_hash(
        {
            "idempotencyKey": "KEY-1",
            "customer": "CODEX",
            "items": [{"sku": "A", "quantity": 2}],
        }
    )
    second = fc.request_hash(
        {
            "items": [{"quantity": 2, "sku": "A"}],
            "customer": "CODEX",
            "idempotencyKey": "KEY-2",
        }
    )
    changed = fc.request_hash(
        {
            "customer": "CODEX",
            "items": [{"sku": "A", "quantity": 3}],
        }
    )

    assert first == second
    assert first != changed
    assert len(first) == 64


def test_line_total_rejects_zero_and_negative_quantities() -> None:
    assert fc.line_total(Decimal("12.345"), 2) == Decimal("24.69")
    for quantity in [0, -1]:
        with pytest.raises(HTTPException) as exc_info:
            fc.line_total(Decimal("12.00"), quantity)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "invalid_quantity"


def test_unit_price_prefers_variant_price_when_present() -> None:
    product = Product(name="CODEX_UNIT_PRODUCT", price=Decimal("100.00"))
    variant = ProductVariant(product_id=product.id, price=Decimal("120.505"))

    assert fc.unit_price(product) == Decimal("100.00")
    assert fc.unit_price(product, variant) == Decimal("120.51")


def test_checkout_financials_discount_total_is_rounded() -> None:
    totals = fc.CheckoutFinancials(
        subtotal=Decimal("100.00"),
        product_discount=Decimal("1.005"),
        coupon_discount=Decimal("2.675"),
        loyalty_discount=Decimal("0.004"),
        shipping_total=Decimal("10.00"),
        total=Decimal("106.32"),
        coupon_id=None,
        shipping_source="unit",
        breakdown={},
    )

    assert totals.discount_total == Decimal("3.68")


@pytest.mark.asyncio
async def test_sync_order_payment_status_maps_paid_and_refunded_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_paid(_session: object, _order_id: object) -> Decimal:
        return Decimal("100.00")

    async def fake_refunded(_session: object, _order_id: object) -> Decimal:
        return Decimal("25.00")

    monkeypatch.setattr(fc, "approved_payment_total", fake_paid)
    monkeypatch.setattr(fc, "refunded_total", fake_refunded)
    order = SimpleNamespace(id="order-1", total=Decimal("100.00"), payment_status="pending")

    await fc.sync_order_payment_status(object(), order)

    assert order.payment_status == "partially_refunded"
