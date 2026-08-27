from backend.app.api.routes.operations import _normalize_store_review_status_payload
from backend.app.services.store_review_compat import (
    _GENERIC_PUBLIC_SQL,
    normalize_store_review_row,
)


def test_admin_approval_payload_also_updates_legacy_status_column() -> None:
    payload = _normalize_store_review_status_payload(
        {"is_approved": True, "is_rejected": False}
    )

    assert payload["status"] == "approved"
    assert payload["is_approved"] is True


def test_generic_public_query_supports_resource_extra_data_schema() -> None:
    assert "sr.extra_data ->> 'rating'" in _GENERIC_PUBLIC_SQL
    assert "sr.extra_data ->> 'is_approved'" in _GENERIC_PUBLIC_SQL
    assert "'accepted'" in _GENERIC_PUBLIC_SQL


def test_accepted_review_status_is_normalized_as_public() -> None:
    review = normalize_store_review_row(
        {
            "id": "review-1",
            "user_id": "user-1",
            "rating": "5",
            "status": "accepted",
            "created_at": "2026-08-26T00:00:00Z",
            "updated_at": "2026-08-26T00:00:00Z",
        }
    )

    assert review["is_approved"] is True
    assert review["is_rejected"] is False
