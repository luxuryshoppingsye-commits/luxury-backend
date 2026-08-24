from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend.app.api.routes.commerce import (
    PRODUCT_IMAGE_REQUIRED_DETAIL,
    _canonicalize_catalog_image,
    _ensure_product_image_for_public_visibility,
    _normalize_public_product_images,
    _row_has_valid_public_primary_image,
)
from backend.app.services import catalog_policy
from backend.app.api.routes import share
from backend.app.models.domain import Product
from PIL import Image
from io import BytesIO


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


def test_public_product_accepts_first_image_when_primary_is_missing(tmp_path) -> None:
    _jpeg(tmp_path / "products" / "product-1.jpg")

    assert _row_has_valid_public_primary_image(
        {
            "image_url": None,
            "images": ["/uploads/products/product-1.jpg"],
        },
        upload_dir=tmp_path,
    )


def test_legacy_cdn_image_is_moved_behind_the_canonical_proxy() -> None:
    row = _normalize_public_product_images(
        {
            "image_url": None,
            "images": [
                "https://images.luxuryshoppings.com/products/item-1.webp",
            ],
        }
    )

    assert row["image_url"] == "/api/catalog/image-proxy/products/item-1.webp"
    assert row["images"] == ["/api/catalog/image-proxy/products/item-1.webp"]


def test_legacy_cdn_image_uses_absolute_api_proxy_in_production(monkeypatch) -> None:
    class ProductionSettings:
        app_env = "production"
        api_base_url = "https://api.luxuryshoppings.com"
        r2_public_base_url = "https://images.luxuryshoppings.com"

    monkeypatch.setattr(catalog_policy, "get_settings", lambda: ProductionSettings())

    assert catalog_policy._public_upload_url(
        "https://images.luxuryshoppings.com/products/item-1.webp"
    ) == "https://api.luxuryshoppings.com/api/catalog/image-proxy/products/item-1.webp"


def test_catalog_proxy_path_uses_absolute_api_url_in_production(monkeypatch) -> None:
    class ProductionSettings:
        app_env = "production"
        api_base_url = "https://api.luxuryshoppings.com"
        r2_public_base_url = "https://images.luxuryshoppings.com"

    monkeypatch.setattr(catalog_policy, "get_settings", lambda: ProductionSettings())

    assert catalog_policy._public_upload_url(
        "/api/catalog/image-proxy/products/item-1.webp"
    ) == "https://api.luxuryshoppings.com/api/catalog/image-proxy/products/item-1.webp"


def test_share_image_reader_allows_configured_api_and_r2_hosts(monkeypatch) -> None:
    class ProductionSettings:
        api_base_url = "https://api.luxuryshoppings.com"
        r2_public_base_url = "https://images.luxuryshoppings.com"

    monkeypatch.setattr(share, "settings", ProductionSettings())

    assert share._allowed_remote_image_hosts() == {
        "api.luxuryshoppings.com",
        "images.luxuryshoppings.com",
    }


def test_canonical_proxy_repairs_jpeg_eoi_and_reports_actual_mime() -> None:
    output = BytesIO()
    Image.new("RGB", (2, 2), (220, 170, 20)).save(output, format="JPEG")
    incomplete = output.getvalue().removesuffix(b"\xff\xd9")

    canonical = _canonicalize_catalog_image(incomplete)

    assert canonical is not None
    data, media_type = canonical
    assert media_type == "image/jpeg"
    assert data.endswith(b"\xff\xd9")


def test_canonical_proxy_transcodes_webp_to_android_safe_jpeg() -> None:
    output = BytesIO()
    Image.new("RGB", (2, 2), (220, 170, 20)).save(output, format="WEBP")

    canonical = _canonicalize_catalog_image(output.getvalue())

    assert canonical is not None
    data, media_type = canonical
    assert media_type == "image/jpeg"
    assert data.startswith(b"\xff\xd8\xff")


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
