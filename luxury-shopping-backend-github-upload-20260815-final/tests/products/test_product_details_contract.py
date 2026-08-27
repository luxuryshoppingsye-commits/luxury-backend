from __future__ import annotations

import uuid
from decimal import Decimal

from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Product, ProductVariant
from backend.app.services.catalog_policy import public_product_response
from backend.app.services.api_protection import policy_for_route


def test_product_review_model_contains_secure_review_fields() -> None:
    columns = set(MODEL_BY_TABLE["product_reviews"].__table__.c.keys())
    assert {
        "user_id",
        "product_id",
        "order_id",
        "rating",
        "comment",
        "review_images",
        "is_verified_purchase",
        "is_approved",
    }.issubset(columns)


def test_public_product_response_exposes_variant_options() -> None:
    product_id = uuid.uuid4()
    product = Product(id=product_id, name="Test product", price=Decimal("10"), images=[])
    variant = ProductVariant(
        product_id=product_id,
        size="M",
        color="Black",
        stock_quantity=3,
        images=[],
    )

    payload = public_product_response(product, variants=[variant])

    assert payload["has_variant_options"] is True
    assert payload["variants"][0]["size"] == "M"


def test_product_details_mutations_require_authentication_policy() -> None:
    for method, path in (
        ("POST", "/api/reviews/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e"),
        ("PATCH", "/api/reviews/11afafe1-dc42-42ee-b3d1-0bd0f871655e"),
        ("DELETE", "/api/reviews/11afafe1-dc42-42ee-b3d1-0bd0f871655e"),
        ("PUT", "/api/engagement/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e/like"),
    ):
        assert policy_for_route(method, path).authentication_required is True
