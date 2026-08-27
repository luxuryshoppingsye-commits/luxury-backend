from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.app.services.category_integrity import normalize_category_mutation_input


def test_category_mutation_input_normalizes_dashboard_aliases() -> None:
    payload = normalize_category_mutation_input(
        {
            "name": " حقائب ",
            "nameEn": "Bags",
            "sortOrder": "3",
            "isActive": "false",
            "isFeatured": "true",
            "bannerTitle": "عروض الحقائب",
        }
    )

    assert payload["name"] == " حقائب "
    assert payload["name_en"] == "Bags"
    assert payload["sort_order"] == 3
    assert payload["is_active"] is False
    assert payload["is_featured"] is True
    assert payload["banner_title"] == "عروض الحقائب"
    assert "nameEn" not in payload
    assert "isActive" not in payload


@pytest.mark.parametrize("payload", [None, [], "category"])
def test_category_mutation_input_rejects_non_object_payload(payload) -> None:
    with pytest.raises(HTTPException) as error:
        normalize_category_mutation_input(payload)

    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_category_payload"


def test_category_mutation_input_rejects_invalid_boolean() -> None:
    with pytest.raises(HTTPException) as error:
        normalize_category_mutation_input({"isActive": "maybe"})

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "invalid_category_boolean"
