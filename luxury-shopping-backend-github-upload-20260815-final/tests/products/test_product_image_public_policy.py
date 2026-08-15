from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend.app.api.routes.commerce import (
    PRODUCT_IMAGE_REQUIRED_DETAIL,
    _ensure_product_image_for_public_visibility,
    _row_has_valid_public_primary_image,
)
from backend.app.models.domain import Product


def _jpeg(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
    return path


def _product(*, image_url=None, active=True, featured=False, approval_status="approved"):
    return Product(
        name="منتج بصورة مطلوبة",
        price=Decimal("10"),
        image_url=image_url,
        is_active=active,
        is_featured=featured,
        approval_status=approval_status,
    )


def test_public_product_requires_local_primary_image(tmp_path) -> None:
    _jpeg(tmp_path / "products" / "product-1.jpg")

    assert _row_has_valid_public_primary_image(
        {"image_url": "/uploads/products/product-1.jpg"},
        upload_dir=tmp_path,
    )


@pytest.mark.parametrize(
    "image_url",
    [
        None,
        "",
        "/uploads/placeholders/product-default.jpg",
        "https://example.com/product.jpg",
        "https://example.supabase.co/storage/v1/object/public/products/product.jpg",
        "/uploads/products/missing.jpg",
    ],
)
def test_public_product_rejects_missing_or_external_primary_image(tmp_path, image_url) -> None:
    assert not _row_has_valid_public_primary_image(
        {"image_url": image_url},
        upload_dir=tmp_path,
    )


def test_public_product_rejects_extension_magic_mismatch(tmp_path) -> None:
    _jpeg(tmp_path / "products" / "product.webp")

    assert not _row_has_valid_public_primary_image(
        {"image_url": "/uploads/products/product.webp"},
        upload_dir=tmp_path,
    )


def test_active_approved_product_without_image_cannot_be_public() -> None:
    product = _product(image_url=None, active=True, approval_status="approved")

    with pytest.raises(HTTPException) as error:
        _ensure_product_image_for_public_visibility(product)

    assert error.value.status_code == 422
    assert error.value.detail == PRODUCT_IMAGE_REQUIRED_DETAIL


def test_draft_product_without_image_can_remain_private() -> None:
    product = _product(image_url=None, active=False, featured=False, approval_status="needs_image_review")

    _ensure_product_image_for_public_visibility(product)
