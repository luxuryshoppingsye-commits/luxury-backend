from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response

from backend.app.api.routes import commerce, operations
from backend.app.models.domain import Product, ProductVariant, UserCart
from backend.app.services import financial_calculator as fc


@pytest.mark.asyncio
@pytest.mark.parametrize("subtotal,fee", [(49999, 5000), (50000, 0), (55000, 0)])
async def test_final_order_shipping_uses_merchandise_threshold(monkeypatch, subtotal, fee):
    zone = SimpleNamespace(id=uuid.uuid4(), fee=5000, is_active=True, deleted_at=None)
    session = SimpleNamespace(get=AsyncMock(return_value=zone))
    monkeypatch.setattr(fc, "_shipping_configuration", AsyncMock(return_value={}))
    financials = await fc.calculate_checkout_financials(
        session,
        user_id=uuid.uuid4(),
        subtotal=Decimal(subtotal),
        apply_free_shipping=True,
        body={"shippingZoneId": str(zone.id), "shippingCost": 0, "subtotal": 999999},
    )
    assert financials.shipping_total == Decimal(fee)
    assert financials.total == Decimal(subtotal + fee)
    assert "subtotal" in financials.breakdown["ignored_client_fields"]


@pytest.mark.asyncio
async def test_shipping_threshold_uses_sale_price_before_coupon(monkeypatch):
    zone = SimpleNamespace(id=uuid.uuid4(), fee=5000, is_active=True, deleted_at=None)
    session = SimpleNamespace(get=AsyncMock(return_value=zone))
    monkeypatch.setattr(fc, "_shipping_configuration", AsyncMock(return_value={}))
    discounted = await fc.calculate_checkout_financials(
        session, user_id=uuid.uuid4(), subtotal=Decimal(60000),
        product_discount=Decimal(20000), body={"shippingZoneId": str(zone.id)},
        apply_free_shipping=True,
    )
    assert discounted.shipping_total == Decimal(5000)
    assert discounted.total == Decimal(45000)


@pytest.mark.asyncio
async def test_other_order_paths_keep_their_existing_shipping_behavior(monkeypatch):
    zone = SimpleNamespace(id=uuid.uuid4(), fee=5000, is_active=True, deleted_at=None)
    session = SimpleNamespace(get=AsyncMock(return_value=zone))
    monkeypatch.setattr(fc, "_shipping_configuration", AsyncMock(return_value={}))
    order = await fc.calculate_checkout_financials(
        session, user_id=uuid.uuid4(), subtotal=Decimal(70000), body={"shippingZoneId": str(zone.id)},
    )
    assert order.shipping_total == Decimal(5000)
    assert order.total == Decimal(75000)


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{"enabled": False}, {"free_shipping_enabled": False}])
async def test_disabled_free_shipping_retains_zone_fee(monkeypatch, config):
    zone = SimpleNamespace(id=uuid.uuid4(), fee=5000, is_active=True, deleted_at=None)
    session = SimpleNamespace(get=AsyncMock(return_value=zone))
    monkeypatch.setattr(fc, "_shipping_configuration", AsyncMock(return_value=config))
    fee, _, policy = await fc._shipping_total(
        session, {"shippingZoneId": str(zone.id)}, subtotal=Decimal(70000),
    )
    assert fee == Decimal(5000)
    assert policy["free_shipping_threshold"] is None


@pytest.mark.asyncio
async def test_saved_shipping_configuration_supports_nested_settings():
    row = SimpleNamespace(extra_data={"setting_value": {"default_fee": 3000, "free_shipping_threshold": 50000}})
    result = SimpleNamespace(scalar_one_or_none=lambda: row)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    fee, source, policy = await fc._shipping_total(session, {}, subtotal=Decimal(55000))
    assert fee == Decimal(0)
    assert source == "default_config"
    assert policy["free_shipping_threshold"] == "50000.00"


@pytest.mark.asyncio
@pytest.mark.parametrize("subtotal,expected", [(40000, "5000.00"), (50000, "0.00"), (55000, "0.00")])
async def test_address_quote_agrees_with_final_order(monkeypatch, subtotal, expected):
    zone = SimpleNamespace(id=uuid.uuid4(), fee=5000, is_active=True, deleted_at=None)
    result = SimpleNamespace(scalars=lambda: [zone])
    session = SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(return_value=zone))
    monkeypatch.setattr(operations, "serialize_record", lambda row: {"name": "صنعاء"})
    monkeypatch.setattr(fc, "_shipping_configuration", AsyncMock(return_value={}))
    request = SimpleNamespace(json=AsyncMock(return_value={"address": "صنعاء - حدة", "subtotal": subtotal}))
    quote = await operations.shipping_quote(request, session)
    assert quote["fee"] == expected
    assert quote["freeShippingThreshold"] == "50000.00"
    assert ("مجاني" in quote["label"]) == (subtotal >= 50000)


@pytest.mark.asyncio
async def test_buy_now_validates_selected_variant_without_reading_cart(monkeypatch):
    product_id, variant_id = uuid.uuid4(), uuid.uuid4()
    product = Product(id=product_id, name="فستان سهرة", price=Decimal(55000), track_inventory=True, stock_quantity=10)
    variant = ProductVariant(id=variant_id, product_id=product_id, price=Decimal(56000), stock_quantity=5)
    results = [SimpleNamespace(scalar_one_or_none=lambda: product), SimpleNamespace(scalar_one_or_none=lambda: variant)]
    session = SimpleNamespace(execute=AsyncMock(side_effect=results))
    eligible = SimpleNamespace(product=product, variant=variant, unit_price=Decimal(56000))
    monkeypatch.setattr(commerce, "eligible_line", AsyncMock(return_value=eligible))
    lines, subtotal, _ = await commerce._validated_cart_lines(
        session, uuid.uuid4(),
        buy_now_item={"productId": str(product_id), "variantId": str(variant_id), "quantity": 1, "price": 1},
    )
    assert len(lines) == 1
    assert lines[0][0].quantity == 1
    assert lines[0][1].id == product_id
    assert lines[0][2].id == variant_id
    assert subtotal == Decimal(56000)
    assert all("user_cart" not in str(call.args[0]).lower() for call in session.execute.await_args_list)


@pytest.mark.parametrize("body", [{"buyNowItem": None}, {"buyNowItem": []}, {"buyNowItem": {}}])
def test_invalid_direct_purchase_cannot_fall_back_to_cart(body):
    with pytest.raises(HTTPException) as error:
        commerce._checkout_buy_now_item(body)
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [0, -1, 1.5])
async def test_buy_now_rejects_invalid_quantity(quantity):
    session = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(HTTPException):
        await commerce._validated_cart_lines(
            session, uuid.uuid4(), buy_now_item={"productId": str(uuid.uuid4()), "quantity": quantity},
        )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("subtotal,expected", [(40000, "5000.00"), (50000, "0.00")])
async def test_cart_quote_without_address_uses_same_shipping_policy(monkeypatch, subtotal, expected):
    result = SimpleNamespace(scalars=lambda: [])
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    monkeypatch.setattr(fc, "_shipping_configuration", AsyncMock(return_value={}))
    request = SimpleNamespace(json=AsyncMock(return_value={"subtotal": subtotal, "address": ""}))
    quote = await operations.shipping_quote(request, session)
    assert quote["fee"] == expected
    assert quote["freeShippingThreshold"] == "50000.00"
    assert quote["isEstimated"] is True


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("buy_now", [False, True])
async def test_only_full_cart_checkout_clears_saved_cart(monkeypatch, buy_now):
    product = SimpleNamespace(id=uuid.uuid4(), name="فستان سهرة", image_url=None, partner_id=None, track_inventory=False)
    item = UserCart(id=uuid.uuid4(), product_id=product.id, user_id=uuid.uuid4(), quantity=1)
    financials = SimpleNamespace(
        subtotal=Decimal(55000), shipping_total=Decimal(0), discount_total=Decimal(0),
        total=Decimal(55000), coupon_id=None, shipping_source="shipping_zone", breakdown={},
    )
    monkeypatch.setattr(commerce, "validate_payment_method_for_checkout", AsyncMock(return_value="cash_on_delivery"))
    monkeypatch.setattr(commerce, "validate_customer_checkout_address", lambda address: address)
    monkeypatch.setattr(commerce, "_validated_cart_lines", AsyncMock(return_value=([(item, product, None, Decimal(55000))], Decimal(55000), Decimal(0))))
    monkeypatch.setattr(commerce, "calculate_checkout_financials", AsyncMock(return_value=financials))
    monkeypatch.setattr(commerce, "_record_financial_side_effects", AsyncMock())
    monkeypatch.setattr(commerce, "_create_notification", AsyncMock())
    monkeypatch.setattr(commerce, "_return_policy_snapshot", lambda *args: {})
    monkeypatch.setattr(commerce, "_serialize_order", lambda order, **kwargs: {"total": str(order.total)})
    session = SimpleNamespace(
        begin_nested=lambda: _Transaction(), add=lambda row: None,
        flush=AsyncMock(), commit=AsyncMock(), execute=AsyncMock(),
    )
    body = {"paymentMethod": "cash_on_delivery", "shippingAddress": {"city": "صنعاء"}}
    if buy_now:
        body["buyNowItem"] = {"productId": str(product.id), "quantity": 1}
    request = SimpleNamespace(json=AsyncMock(return_value=body))
    order = await commerce.checkout(request, Response(), None, SimpleNamespace(id=item.user_id), session)
    assert order["total"] == "55000"
    assert session.execute.await_count == (0 if buy_now else 1)
    session.commit.assert_awaited_once()
