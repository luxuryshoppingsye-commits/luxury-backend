from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend.app.api.routes.commerce import (
    PRODUCT_CONTENT_REQUIRED_DETAIL,
    _ensure_product_content_for_public_visibility,
    _row_has_safe_public_product_text,
    _safe_public_display_text,
)
from backend.app.services.catalog_policy import is_public_approval_status, is_public_product
from backend.app.models.domain import Product


def _product(
    *,
    name: str,
    name_en: str | None = None,
    active: bool = True,
    featured: bool = False,
    approval_status: str | None = "approved",
) -> Product:
    return Product(
        name=name,
        name_en=name_en,
        price=Decimal("10"),
        image_url="/uploads/products/official-product.jpg",
        is_active=active,
        is_featured=featured,
        approval_status=approval_status,
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Imported product",
        "Unknown product",
        "Product e22e2497db",
        "CODEX_AD_banner",
        "CODEX_FINANCIAL_TEST_item",
        "CODEX_CUSTOMER_E2E_product",
        "Summer TEST product",
        "Visible RUN_ID product",
        "9b65f599-0f01-4b24-8b0c-dcb409721885",
    ],
)
def test_internal_or_generic_visible_product_text_is_not_public(value: str) -> None:
    assert not _safe_public_display_text(value)
    assert not _row_has_safe_public_product_text({"name": value, "name_en": None})


def test_public_product_can_use_verified_english_name_when_arabic_name_is_missing() -> None:
    assert _row_has_safe_public_product_text(
        {"name": "", "name_en": "Luxury leather handbag"}
    )


@pytest.mark.parametrize(
    "name",
    [
        "Imported product",
        "Product e22e2497db",
        "CODEX_CUSTOMER_E2E_product",
    ],
)
def test_active_approved_product_with_internal_name_cannot_be_public(name: str) -> None:
    product = _product(name=name)

    with pytest.raises(HTTPException) as error:
        _ensure_product_content_for_public_visibility(product)

    assert error.value.status_code == 422
    assert error.value.detail == PRODUCT_CONTENT_REQUIRED_DETAIL


def test_private_product_with_incomplete_content_can_remain_for_admin_review() -> None:
    product = _product(
        name="Imported product",
        active=False,
        featured=False,
        approval_status="needs_content_review",
    )

    _ensure_product_content_for_public_visibility(product)


def test_legacy_product_without_explicit_approval_status_remains_public() -> None:
    product = _product(name="Legacy imported luxury item", approval_status=None)

    assert is_public_approval_status(product.approval_status)
    assert is_public_product(product)


@pytest.mark.parametrize("approval_status", ["approved", "accepted", "active", "published", "visible", "live", "", None])
def test_supported_public_product_statuses_are_visible(approval_status: str | None) -> None:
    product = _product(name="Visible luxury catalog item", approval_status=approval_status)

    assert is_public_approval_status(product.approval_status)
    assert is_public_product(product)


@pytest.mark.parametrize("approval_status", ["legacy_imported", "approved_by_admin", "available"])
def test_legacy_non_private_approval_statuses_remain_visible(approval_status: str) -> None:
    product = _product(name="Legacy visible catalog item", approval_status=approval_status)

    assert is_public_approval_status(product.approval_status)
    assert is_public_product(product)


def test_inactive_legacy_approved_product_can_still_be_visible_in_catalog() -> None:
    product = _product(name="Approved catalog item", active=False, approval_status="approved")

    assert is_public_product(product)


@pytest.mark.parametrize(
    "approval_status",
    [
        "pending",
        "pending_approval",
        "reviewing",
        "under_review",
        "rejected",
        "inactive",
        "disabled",
        "deleted",
        "needs_image_review",
        "needs_content_review",
        "not_approved",
        "unapproved",
    ],
)
def test_explicit_private_product_statuses_remain_hidden(approval_status: str) -> None:
    product = _product(name="Private catalog item", approval_status=approval_status)

    assert not is_public_approval_status(product.approval_status)
    assert not is_public_product(product)
