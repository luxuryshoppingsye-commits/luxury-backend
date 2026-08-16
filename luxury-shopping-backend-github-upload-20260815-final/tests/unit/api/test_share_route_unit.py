from app.api.routes import share


def test_share_html_contains_product_open_graph_and_json_ld() -> None:
    original_api = share.settings.api_base_url
    original_frontend = share.settings.frontend_public_url
    original_render = share.settings.render_public_url
    share.settings.api_base_url = "https://api.luxuryshoppings.com"
    share.settings.frontend_public_url = "https://luxuryshoppings.com"
    share.settings.render_public_url = "https://luxury-backend-xy9d.onrender.com"
    try:
        html = share._build_share_html(
            "product-123",
            {
                "name": "ساعة فاخرة",
                "description": "ساعة أصلية",
                "price": "1000.00",
                "currency_code": "YER",
                "is_orderable": True,
            },
        )
    finally:
        share.settings.api_base_url = original_api
        share.settings.frontend_public_url = original_frontend
        share.settings.render_public_url = original_render

    assert 'property="og:type" content="product"' in html
    assert 'property="og:title" content="ساعة فاخرة | رفاهية التسوق"' in html
    assert 'property="og:image" content="https://luxury-backend-xy9d.onrender.com/share/products/product-123/image"' in html
    assert '"@type": "Product"' in html
    assert "https://luxuryshoppings.com/p/product-123" in html


def test_image_signature_detection_rejects_html() -> None:
    assert share._detect_image_type(b"\xff\xd8\xff\xe0image") == "image/jpeg"
    assert share._detect_image_type(b"\x89PNG\r\n\x1a\nimage") == "image/png"
    assert share._detect_image_type(b"<html>not an image</html>") is None


def test_share_identifier_rejects_path_traversal() -> None:
    assert share._identifier("4f7bbdb4-0782-4b90-9f5c-a4c7cbad0bb4")
    try:
        share._identifier("../secret")
    except Exception as error:
        assert getattr(error, "status_code", None) == 404
    else:
        raise AssertionError("path traversal identifier must be rejected")
