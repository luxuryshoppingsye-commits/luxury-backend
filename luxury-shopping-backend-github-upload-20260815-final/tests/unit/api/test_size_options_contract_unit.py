from backend.app.api.routes.operations import _normalize_size_option_body, _serialize_size_option_payload


def test_size_option_body_normalizes_category_and_comma_separated_values() -> None:
    payload = _normalize_size_option_body(
        {"category_id": "women-clothes", "sizes": "S, M, S", "sort_order": 2}
    )

    assert payload["category_type"] == "women-clothes"
    assert payload["name"] == "women-clothes"
    assert payload["sizes"] == ["S", "M"]
    assert payload["status"] == "active"


def test_size_option_response_has_canonical_shape_for_legacy_records() -> None:
    payload = _serialize_size_option_payload(
        {"id": "size-1", "name": "أحذية", "values": "38, 39", "sort_order": 0}
    )

    assert payload["category_type"] == "أحذية"
    assert payload["sizes"] == ["38", "39"]
