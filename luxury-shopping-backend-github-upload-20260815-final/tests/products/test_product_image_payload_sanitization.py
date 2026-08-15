from backend.app.api.routes.commerce import _hide_placeholder_product_images


def test_product_payload_hides_shared_placeholder_image() -> None:
    row = {
        "id": "product-1",
        "image_url": "/uploads/placeholders/product-default.jpg",
        "imageUrl": "/uploads/placeholders/product-default.jpg",
        "images": [
            "/uploads/placeholders/product-default.jpg",
            {"url": "/uploads/placeholders/product-default.jpg"},
            "/uploads/products/product-1-1.jpg",
        ],
    }

    sanitized = _hide_placeholder_product_images(row)

    assert sanitized["image_url"] is None
    assert sanitized["imageUrl"] is None
    assert sanitized["images"] == ["/uploads/products/product-1-1.jpg"]


def test_product_payload_keeps_product_owned_image() -> None:
    row = {
        "id": "product-2",
        "image_url": "/uploads/products/product-2-1.jpg",
        "imageUrl": "/uploads/products/product-2-1.jpg",
        "images": ["/uploads/products/product-2-1.jpg"],
    }

    sanitized = _hide_placeholder_product_images(row)

    assert sanitized["image_url"] == "/uploads/products/product-2-1.jpg"
    assert sanitized["imageUrl"] == "/uploads/products/product-2-1.jpg"
    assert sanitized["images"] == ["/uploads/products/product-2-1.jpg"]


def test_manage_product_image_urls_normalize_api_upload_prefix() -> None:
    from backend.app.api.routes.commerce import _resolve_manage_product_image_urls

    row = {
        "image_url": "/api/uploads/products/legacy-1.webp",
        "images": ["backend/data/uploads/products/legacy-2.webp"],
    }

    resolved = _resolve_manage_product_image_urls(row)

    assert resolved["image_url"] == "/uploads/products/legacy-1.webp"
    assert resolved["images"] == ["/uploads/products/legacy-2.webp"]
