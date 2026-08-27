from backend.app.api.routes.operations import _enrich_inventory_payload


def test_inventory_payload_includes_product_and_warehouse_details() -> None:
    payload = _enrich_inventory_payload(
        {
            "id": "location-1",
            "product_id": "product-1",
            "warehouse_id": "warehouse-1",
            "quantity": "12",
        },
        {"product-1": {"id": "product-1", "name": "حقيبة"}},
        {"warehouse-1": {"id": "warehouse-1", "name": "المستودع الرئيسي"}},
    )

    assert payload["product"]["name"] == "حقيبة"
    assert payload["warehouse"]["name"] == "المستودع الرئيسي"
    assert payload["quantity"] == 12


def test_movement_payload_exposes_type_and_signed_quantity() -> None:
    payload = _enrich_inventory_payload(
        {
            "product_id": "product-1",
            "warehouse_id": "warehouse-1",
            "type": "out",
            "quantity": 3,
        },
        {},
        {},
        movement=True,
    )

    assert payload["movement_type"] == "out"
    assert payload["signed_quantity"] == -3
