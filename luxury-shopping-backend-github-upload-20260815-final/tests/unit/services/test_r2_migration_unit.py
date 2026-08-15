from backend.app.services.r2_migration import R2MigrationService, _replacement_map, _replace_exact


def test_replace_exact_preserves_unrelated_values_and_nested_shape():
    replacements = {"/uploads/products/old.webp": "https://images.luxuryshoppings.com/products/old.webp"}
    value = {
        "image_url": "/uploads/products/old.webp",
        "images": ["/uploads/products/old.webp", "https://example.com/other.webp"],
        "metadata": {"caption": "old.webp"},
    }

    updated, changed = _replace_exact(value, replacements)

    assert changed == 2
    assert updated["image_url"] == "https://images.luxuryshoppings.com/products/old.webp"
    assert updated["images"][0] == "https://images.luxuryshoppings.com/products/old.webp"
    assert updated["images"][1] == "https://example.com/other.webp"
    assert updated["metadata"]["caption"] == "old.webp"


def test_legacy_product_image_directory_is_normalized_to_r2_key():
    assert (
        R2MigrationService._legacy_key_from_reference(
            "https://luxury-backend-34ht.onrender.com/uploads/product-images/item.webp"
        )
        == "products/item.webp"
    )


def test_replacement_map_handles_legacy_testserver_origin():
    replacements = _replacement_map(
        "avatars/avatar.png", "https://images.luxuryshoppings.com"
    )
    assert replacements["http://testserver/uploads/avatars/avatar.png"] == (
        "https://images.luxuryshoppings.com/avatars/avatar.png"
    )
