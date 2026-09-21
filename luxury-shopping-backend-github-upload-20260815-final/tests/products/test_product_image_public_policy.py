from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend.app.api.routes.commerce import (
    PRODUCT_IMAGE_REQUIRED_DETAIL,
    _catalog_image_cache_get,
    _catalog_image_cache_put,
    _catalog_image_response,
    _catalog_image_variant_key,
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
from starlette.requests import Request


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


def test_public_r2_image_stays_on_the_fast_cdn_path() -> None:
    row = _normalize_public_product_images(
        {
            "image_url": None,
            "images": [
                "https://images.luxuryshoppings.com/products/item-1.webp",
            ],
        }
    )

    assert row["image_url"] == "https://images.luxuryshoppings.com/products/item-1.webp"
    assert row["images"] == ["https://images.luxuryshoppings.com/products/item-1.webp"]


def test_public_r2_image_uses_direct_cdn_url_in_production(monkeypatch) -> None:
    class ProductionSettings:
        app_env = "production"
        api_base_url = "https://api.luxuryshoppings.com"
        r2_public_base_url = "https://images.luxuryshoppings.com"

    monkeypatch.setattr(catalog_policy, "get_settings", lambda: ProductionSettings())

    assert catalog_policy._public_upload_url(
        "https://images.luxuryshoppings.com/products/item-1.webp"
    ) == "https://images.luxuryshoppings.com/products/item-1.webp"


def test_catalog_proxy_path_uses_absolute_api_url_in_production(monkeypatch) -> None:
    class ProductionSettings:
        app_env = "production"
        api_base_url = "https://api.luxuryshoppings.com"
        r2_public_base_url = "https://images.luxuryshoppings.com"

    monkeypatch.setattr(catalog_policy, "get_settings", lambda: ProductionSettings())

    assert catalog_policy._public_upload_url(
        "/api/catalog/image-proxy/products/item-1.webp"
    ) == "https://api.luxuryshoppings.com/api/catalog/image-proxy/products/item-1.webp"


def test_brand_file_reference_uses_public_checked_logo_route() -> None:
    asset_id = uuid.uuid4()

    assert catalog_policy.public_brand_logo_url(f"file:{asset_id}") == (
        f"/api/catalog/brand-logo/{asset_id}"
    )


def test_brand_legacy_site_asset_path_is_normalized() -> None:
    assert catalog_policy.public_brand_logo_url("site-assets/brands/chanel.webp") == (
        "/uploads/site-assets/brands/chanel.webp"
    )


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


def test_catalog_proxy_builds_small_card_variant() -> None:
    output = BytesIO()
    Image.new("RGB", (1600, 1200), (220, 170, 20)).save(output, format="JPEG", quality=95)

    canonical = _canonicalize_catalog_image(output.getvalue(), max_width=640, quality=80)

    assert canonical is not None
    data, media_type = canonical
    assert media_type == "image/jpeg"
    with Image.open(BytesIO(data)) as image:
        assert image.size == (640, 480)


def _request(*, if_none_match: str | None = None) -> Request:
    headers = []
    if if_none_match:
        headers.append((b"if-none-match", if_none_match.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/catalog/image-proxy/products/item.webp",
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_catalog_proxy_cache_reuses_variant_and_supports_conditional_get() -> None:
    key = _catalog_image_variant_key("products/cache-contract.webp", 640, 80)
    entry = (b"cached-image", "image/jpeg", '"stable-etag"')

    _catalog_image_cache_put(key, entry)

    assert _catalog_image_cache_get(key) == entry
    response = _catalog_image_response(
        _request(if_none_match='"stable-etag"'),
        entry,
        cache_status="HIT",
    )
    assert response.status_code == 304
    assert response.headers["etag"] == '"stable-etag"'
    assert response.headers["x-image-cache"] == "HIT"
    assert response.headers["cache-control"] == (
        "public, max-age=31536000, s-maxage=31536000, immutable"
    )


def test_share_image_builds_small_card_variant() -> None:
    output = BytesIO()
    Image.new("RGB", (1200, 1600), (220, 170, 20)).save(output, format="PNG")

    variant = share._share_image_variant(output.getvalue(), max_width=640, quality=80)

    assert variant is not None
    data, media_type = variant
    assert media_type == "image/jpeg"
    with Image.open(BytesIO(data)) as image:
        assert image.size == (640, 853)


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
