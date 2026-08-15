from __future__ import annotations

from backend.app.services.store_review_compat import normalize_store_review_row


def test_normalize_store_review_prefers_customer_name_and_comment():
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "user_id": "22222222-2222-2222-2222-222222222222",
        "rating": 5,
        "comment": "Great service",
        "customer_name": "Customer A",
        "profile_full_name": "Profile Name",
        "is_approved": True,
        "is_rejected": False,
        "admin_notes": None,
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    normalized = normalize_store_review_row(row)
    assert normalized["customer_name"] == "Customer A"
    assert normalized["comment"] == "Great service"
    assert normalized["is_approved"] is True


def test_normalize_store_review_status_schema():
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "user_id": "22222222-2222-2222-2222-222222222222",
        "rating": 4,
        "body": "Solid",
        "customer_name": None,
        "profile_full_name": "Fallback User",
        "status": "approved",
        "is_rejected": False,
        "admin_notes": None,
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    normalized = normalize_store_review_row(row)
    assert normalized["customer_name"] == "Fallback User"
    assert normalized["comment"] == "Solid"
    assert normalized["is_approved"] is True
