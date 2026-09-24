from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend.app.api.routes.commerce import _partner_draft_payload
from backend.app.models.domain import Product
from backend.app.services.catalog_policy import normalize_product_mutation_values, public_product_response
from backend.app.services.resource_policy import response_field_allowed


def test_purchase_cost_is_separate_from_compare_at_price() -> None:
    values = normalize_product_mutation_values({
        "name": "فستان صنعاني",
        "price": 2000,
        "original_price": 2200,
        "cost_price": 1800,
    })
    assert values["price"] == Decimal("2000.00")
    assert values["original_price"] == Decimal("2200.00")
    assert values["cost_price"] == Decimal("1800.00")


@pytest.mark.parametrize("cost", [0, 2000, 2100])
def test_purchase_cost_must_be_positive_and_lower_than_sale_price(cost: int) -> None:
    with pytest.raises(HTTPException) as error:
        normalize_product_mutation_values({"name": "فستان صنعاني", "price": 2000, "cost_price": cost})
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "invalid_cost_price"


def test_purchase_cost_is_private_and_allowed_in_partner_draft() -> None:
    product = Product(name="فستان صنعاني", price=Decimal("2000"), cost_price=Decimal("1800"))
    assert "cost_price" not in public_product_response(product)
    assert not response_field_allowed("products", "cost_price", {"customer"}, None)
    draft = _partner_draft_payload({
        "formData": {"name": "فستان صنعاني", "costPrice": "1800"},
        "productImages": [],
        "arImageUrl": "",
    })
    assert draft["formData"]["costPrice"] == "1800"
