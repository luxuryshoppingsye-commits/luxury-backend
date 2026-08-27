from __future__ import annotations

from backend.app.api.routes.operations import _normalize_admin_body
from backend.app.repositories.resources import ResourceRepository
from backend.app.services.api_protection import policy_for_route


def test_supplier_create_normalizes_shared_dashboard_fields() -> None:
    payload = _normalize_admin_body(
        "suppliers",
        {
            "business_name": " متجر تجريبي ",
            "supplierType": "merchant",
            "isActive": True,
            "images": "https://images.example/store.webp",
            "whatsappNumber": "+967777000111",
        },
        for_create=True,
    )

    assert payload["name"] == "متجر تجريبي"
    assert payload["supplier_type"] == "merchant"
    assert payload["status"] == "active"
    assert payload["images"] == ["https://images.example/store.webp"]
    assert payload["whatsapp_number"] == "+967777000111"


def test_supplier_update_does_not_reactivate_without_status_input() -> None:
    payload = _normalize_admin_body("suppliers", {"description": "updated"})

    assert payload == {"description": "updated"}


def test_api_storage_compatibility_alias_uses_upload_policy() -> None:
    policy = policy_for_route("POST", "/api/storage/upload")

    assert policy.policy_name == "upload"
    assert policy.authentication_required is True
    assert policy.idempotency_required is False


def test_courier_create_normalizes_name_and_active_status() -> None:
    payload = _normalize_admin_body(
        "couriers",
        {"full_name": " مندوب تجريبي ", "is_active": True},
        for_create=True,
    )

    assert payload["name"] == "مندوب تجريبي"
    assert payload["status"] == "active"


def test_generic_courier_writer_uses_canonical_columns() -> None:
    repository = ResourceRepository(None, "couriers", None, {"admin"})

    prepared = repository._prepare_data(
        {
            "full_name": "مندوب الموارد",
            "phone": "+967777000111",
            "is_active": False,
            "vehicle_type": "motorcycle",
        },
        "insert",
    )

    assert prepared["name"] == "مندوب الموارد"
    assert prepared["status"] == "inactive"
    assert prepared["extra_data"]["full_name"] == "مندوب الموارد"
