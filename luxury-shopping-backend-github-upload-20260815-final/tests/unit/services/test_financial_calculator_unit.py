from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.app.models import MODEL_BY_TABLE
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


@pytest.mark.parametrize(
    ("total", "paid", "expected"),
    [
        ("100.00", "0", "unpaid"),
        ("100.00", "25.00", "partial"),
        ("100.00", "100.00", "paid"),
        ("100.00", "125.00", "paid"),
    ],
)
def test_derive_local_payment_status_from_confirmed_payments(
    total: str,
    paid: str,
    expected: str,
) -> None:
    assert fc.derive_local_payment_status(total, paid) == expected


def test_local_request_total_prefers_final_price_then_estimate_then_amount() -> None:
    assert fc.local_request_total({"final_price": "125.00", "estimated_price": "150.00", "amount": "100.00"}) == Decimal("125.00")
    assert fc.local_request_total({"final_price": "0", "estimated_price": "150.00", "amount": "100.00"}) == Decimal("150.00")
    assert fc.local_request_total({"amount": "100.00"}) == Decimal("100.00")


@pytest.mark.asyncio
async def test_local_request_serializer_uses_confirmed_payment_ledger() -> None:
    request_id = uuid.uuid4()
    request_model = MODEL_BY_TABLE["local_shopping_requests"]
    request = request_model(
        id=request_id,
        user_id=uuid.uuid4(),
        status="shipping",
        description="منتج محلي للاختبار",
        amount=Decimal("100.00"),
        extra_data={"final_price": "200.00"},
    )

    class Result:
        def all(self):
            return [(str(request_id), Decimal("200.00"))]

    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    rows = await fc.serialize_local_shopping_requests(session, [request])

    assert rows[0]["payment_status"] == "paid"
    assert rows[0]["paid_amount"] == "200.00"
    assert rows[0]["remaining_balance"] == "0.00"


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


class _CouponLookupResult:
    def __init__(self, coupon: object) -> None:
        self._coupon = coupon

    def scalar_one_or_none(self) -> object:
        return self._coupon


@pytest.mark.asyncio
async def test_partner_product_coupon_only_discounts_its_eligible_product() -> None:
    coupon = SimpleNamespace(
        id=uuid.uuid4(),
        code="BACK20",
        amount=Decimal("20.00"),
        expires_at=None,
        extra_data={
            "partner_id": "merchant-1",
            "scope": "products",
            "product_ids": ["product-1"],
            "discount_type": "percentage",
            "discount_value": 20,
            "minimum_order_amount": 100,
        },
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_CouponLookupResult(coupon)))
    product = SimpleNamespace(id="product-1", partner_id="merchant-1", category_id="category-1")
    unrelated_product = SimpleNamespace(id="product-2", partner_id="merchant-2", category_id="category-1")

    discount, coupon_id, meta = await fc._coupon_discount(
        session,
        code="BACK20",
        subtotal=Decimal("2500.00"),
        user_id=uuid.uuid4(),
        coupon_lines=[
            (product, 1, Decimal("1000.00")),
            (unrelated_product, 1, Decimal("1500.00")),
        ],
    )

    assert discount == Decimal("200.00")
    assert coupon_id == str(coupon.id)
    assert meta["eligible_subtotal"] == "1000.00"


@pytest.mark.asyncio
async def test_partner_product_coupon_rejects_a_cart_without_its_product() -> None:
    coupon = SimpleNamespace(
        id=uuid.uuid4(),
        code="BACK20",
        amount=Decimal("20.00"),
        expires_at=None,
        extra_data={
            "partner_id": "merchant-1",
            "scope": "products",
            "product_ids": ["product-1"],
            "discount_type": "percentage",
            "discount_value": 20,
        },
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_CouponLookupResult(coupon)))
    unrelated_product = SimpleNamespace(id="product-2", partner_id="merchant-2", category_id="category-1")

    with pytest.raises(HTTPException) as exc_info:
        await fc._coupon_discount(
            session,
            code="BACK20",
            subtotal=Decimal("1500.00"),
            user_id=uuid.uuid4(),
            coupon_lines=[(unrelated_product, 1, Decimal("1500.00"))],
        )

    assert exc_info.value.detail == "coupon_not_applicable"
