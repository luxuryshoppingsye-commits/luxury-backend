from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.routes import commerce
from app.services.merchant_pricing import contract_share, retail_price


@pytest.mark.parametrize(("share", "currency", "expected"), [
    ("85", "YER", "10000"), ("80", "YER", "10700"), ("100", "USD", "8500"),
    ("80", "USD", "10625"),
])
def test_retail_price_uses_contract_share_and_currency_rounding(share, currency, expected):
    assert retail_price(Decimal("8500"), Decimal(share), currency) == Decimal(expected)


def test_zero_share_does_not_publish_an_unrecoverable_net_amount():
    with pytest.raises(HTTPException):
        retail_price(Decimal("8500"), Decimal(0), "YER")


@pytest.mark.asyncio
async def test_web_retail_and_mobile_net_survive_repeated_edits(monkeypatch):
    contract = SimpleNamespace(extra_data={"commissionRate": 85})
    monkeypatch.setattr(commerce, "latest_partner_contract", AsyncMock(return_value=contract))
    net = Decimal("8500")
    args = (None, None)
    created = await commerce._merchant_priced_values(*args, {"price": net}, net_input=True)
    product = SimpleNamespace(**created)
    for _ in range(2):
        visitor = {"price": product.price}
        merchant = commerce._merchant_edit_prices(visitor.copy(), product, contract_share(contract))
        assert Decimal(merchant["price"]) == net
        repeated = await commerce._merchant_priced_values(*args, {"price": net}, net_input=True, existing=product)
        assert product.price == repeated["price"] == Decimal("10000")
        retail = await commerce._merchant_priced_values(*args, visitor, net_input=False, existing=product)
        assert retail == {"price": Decimal("10000")}
    legacy = SimpleNamespace(price=Decimal("999"), extra_data={})
    displayed = commerce._merchant_edit_prices({}, legacy, contract_share(contract))
    unchanged = await commerce._merchant_priced_values(*args, {"price": Decimal(displayed["price"])}, net_input=True, existing=legacy)
    assert unchanged["price"] == legacy.price
