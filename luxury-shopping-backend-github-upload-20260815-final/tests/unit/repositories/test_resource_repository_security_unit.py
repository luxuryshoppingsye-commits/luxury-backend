from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from backend.app.models import MODEL_BY_TABLE
from backend.app.repositories.resources import ResourceRepository
from backend.app.services.resource_policy import normalize_conflict_target, validate_conflict_target


def _repository(table: str, *, roles: set[str] | None = None) -> ResourceRepository:
    return ResourceRepository(
        session=object(),
        table=table,
        user_id=uuid.uuid4(),
        roles=roles or {"customer"},
    )


def _assert_http_detail(exc: pytest.ExceptionInfo[HTTPException], detail: str) -> None:
    assert exc.value.detail == detail


def test_customer_cannot_mutate_order_status_or_financial_fields() -> None:
    repo = _repository("orders")

    with pytest.raises(HTTPException) as exc:
        repo.ensure_access("update")

    _assert_http_detail(exc, "resource_write_policy_denied:orders")


def test_customer_cannot_write_loyalty_balance_or_points_transactions() -> None:
    loyalty_repo = _repository("user_loyalty")
    points_repo = _repository("points_transactions")

    with pytest.raises(HTTPException) as loyalty_exc:
        loyalty_repo.ensure_access("upsert")
    with pytest.raises(HTTPException) as points_exc:
        points_repo.ensure_access("insert")

    _assert_http_detail(loyalty_exc, "resource_write_policy_denied:user_loyalty")
    _assert_http_detail(points_exc, "resource_write_policy_denied:points_transactions")


def test_customer_cannot_approve_review_or_set_moderation_fields() -> None:
    repo = _repository("product_reviews")

    with pytest.raises(HTTPException) as exc:
        repo._prepare_data({"product_id": str(uuid.uuid4()), "is_approved": True}, "insert")

    _assert_http_detail(exc, "protected_mutation_field:product_reviews:is_approved")


def test_customer_review_insert_forces_pending_status() -> None:
    repo = _repository("product_reviews")
    product_id = uuid.uuid4()

    data = repo._prepare_data({"product_id": str(product_id), "title": "T", "body": "B"}, "insert")

    assert data["user_id"] == repo.user_id
    assert data["product_id"] == product_id
    assert data["status"] == "pending"


def test_partner_cannot_mutate_wallet_or_product_owner_field() -> None:
    wallet_repo = _repository("partner_wallets", roles={"partner"})
    product_repo = _repository("products", roles={"partner"})

    with pytest.raises(HTTPException) as wallet_exc:
        wallet_repo.ensure_access("update")
    with pytest.raises(HTTPException) as product_exc:
        product_repo._prepare_data({"partner_id": str(uuid.uuid4()), "name": "P"}, "update")

    _assert_http_detail(wallet_exc, "resource_write_policy_denied:partner_wallets")
    assert str(product_exc.value.detail).startswith("owner_field_mismatch:partner_id")


@pytest.mark.parametrize(
    "table",
    [
        "orders",
        "order_items",
        "order_payments",
        "payment_receipts",
        "order_status_history",
        "order_shipping",
        "shipping_history",
    ],
)
def test_partner_cannot_read_sensitive_order_tables_through_generic_resource(table: str) -> None:
    repo = _repository(table, roles={"partner"})

    with pytest.raises(HTTPException) as exc:
        repo.ensure_access("select")

    _assert_http_detail(exc, "merchant_typed_endpoint_required")


def test_marketer_cannot_create_commissions_or_payments() -> None:
    commissions = _repository("marketer_commissions", roles={"marketer"})
    payments = _repository("marketer_payments", roles={"marketer"})

    with pytest.raises(HTTPException) as commissions_exc:
        commissions.ensure_access("insert")
    with pytest.raises(HTTPException) as payments_exc:
        payments.ensure_access("insert")

    _assert_http_detail(commissions_exc, "resource_write_policy_denied:marketer_commissions")
    _assert_http_detail(payments_exc, "resource_write_policy_denied:marketer_payments")


def test_on_conflict_rejects_unregistered_customer_target() -> None:
    with pytest.raises(HTTPException) as exc:
        validate_conflict_target(
            "wishlist",
            normalize_conflict_target("id,user_id"),
            {"customer"},
        )

    assert str(exc.value.detail).startswith("on_conflict_policy_denied:wishlist")


def test_or_filter_and_not_operators_build_sql_clauses() -> None:
    repo = _repository("products", roles=set())
    or_clause = repo._filter_clause(
        {"column": "_or", "operator": "or", "value": "name.ilike.%bag%,description.ilike.%bag%"}
    )
    not_is_clause = repo._filter_clause({"column": "partner_id", "operator": "not_is", "value": None})
    not_in_clause = repo._filter_clause({"column": "name", "operator": "not_in", "value": ["A", "B"]})

    assert " OR " in str(or_clause)
    assert "IS NOT NULL" in str(not_is_clause)
    assert "NOT IN" in str(not_in_clause)


def test_select_projection_filters_unrequested_and_internal_fields() -> None:
    repo = _repository("products", roles=set())
    product = MODEL_BY_TABLE["products"](
        name="Visible",
        price=10,
        min_stock_quantity=7,
        approval_notes="internal",
    )

    row = repo._serialize_response(product, {"name"})

    assert row == {"name": "Visible"}


def test_non_staff_response_hides_internal_audit_fields() -> None:
    repo = _repository("product_reviews", roles=set())
    review = MODEL_BY_TABLE["product_reviews"](
        user_id=repo.user_id,
        product_id=uuid.uuid4(),
        status="pending",
        title="Visible",
        body="Body",
    )

    row = repo._serialize_response(review)

    assert row["title"] == "Visible"
    assert "deleted_at" not in row
    assert "reviewed_by" not in row


def test_select_internal_field_is_denied_for_public_roles() -> None:
    repo = _repository("products", roles=set())

    with pytest.raises(HTTPException) as exc:
        repo._filter_clause({"column": "min_stock_quantity", "operator": "eq", "value": 1})

    _assert_http_detail(exc, "filter_field_denied:products:min_stock_quantity")


def test_non_staff_can_filter_visible_rows_by_deleted_at_null_only() -> None:
    repo = _repository("product_reviews", roles=set())

    clause = repo._filter_clause({"column": "deleted_at", "operator": "is", "value": None})

    assert "deleted_at IS NULL" in str(clause)


def test_non_staff_cannot_filter_other_internal_audit_fields() -> None:
    repo = _repository("product_reviews", roles=set())

    with pytest.raises(HTTPException) as exc:
        repo._filter_clause({"column": "reviewed_by", "operator": "is", "value": None})

    _assert_http_detail(exc, "filter_field_denied:product_reviews:reviewed_by")
