from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException


ADMIN_ROLES = frozenset({"admin", "manager"})
STAFF_ROLES = frozenset({"admin", "manager", "finance", "logistics", "staff", "employee"})
MUTATING_OPERATIONS = frozenset({"insert", "update", "upsert", "delete"})
GENERIC_MESSAGING_MUTATION_BLOCKED = frozenset(
    {
        "notification_outbox",
        "notification_delivery_attempts",
        "email_outbox",
        "whatsapp_outbox",
        "push_tokens",
        "web_push_subscriptions",
        "admin_notifications",
        "marketing_campaigns",
        "report_exports",
        "financial_reports",
        "courier_location_updates",
        "theme_settings",
        "sync_events",
        "client_mutations",
        "sync_dead_letters",
        "backup_records",
        "support_tickets",
        "ticket_messages",
        "operational_days",
        "form_settings",
    }
)
GENERIC_MESSAGING_SELECT_BLOCKED = frozenset(
    {
        "notification_outbox",
        "notification_delivery_attempts",
        "email_outbox",
        "whatsapp_outbox",
        "push_tokens",
        "web_push_subscriptions",
        "report_exports",
        "financial_reports",
        "sync_events",
        "client_mutations",
        "sync_dead_letters",
        "backup_records",
    }
)

OWNER_FIELDS = frozenset({"user_id", "owner_id", "partner_id", "merchant_id", "marketer_id", "courier_id"})
AUDIT_FIELDS = frozenset(
    {
        "created_by",
        "updated_by",
        "deleted_by",
        "approved_by",
        "reviewed_by",
        "processed_by",
        "signed_by_admin",
        "created_at",
        "updated_at",
        "deleted_at",
        "approved_at",
        "reviewed_at",
        "processed_at",
        "paid_at",
    }
)
SECRET_FIELDS = frozenset(
    {
        "password_hash",
        "password_salt",
        "token_hash",
        "refresh_token",
        "refresh_token_hash",
        "access_token",
        "private_key",
        "secret",
        "auth",
        "p256dh",
        "token",
    }
)
INTERNAL_RESPONSE_FIELDS = SECRET_FIELDS | frozenset(
    {
        "admin_notes",
        "internal_notes",
        "moderation_notes",
        "approval_notes",
        "rejected_reason",
        "payment_reference",
        "supplier_cost",
        "cost_price",
        "minimum_stock",
        "min_stock_quantity",
        "extra_data",
    }
)
INTERNAL_AUDIT_RESPONSE_FIELDS = AUDIT_FIELDS - frozenset({"created_at", "updated_at"})
INTERNAL_RESPONSE_FIELDS = INTERNAL_RESPONSE_FIELDS | INTERNAL_AUDIT_RESPONSE_FIELDS
PUBLIC_PRODUCT_RESPONSE_FIELDS = frozenset(
    {
        "id",
        "short_code",
        "sku",
        "name",
        "name_en",
        "description",
        "rich_description",
        "price",
        "original_price",
        "currency_code",
        "stock_quantity",
        "stock_status",
        "is_featured",
        "category_id",
        "brand_id",
        "partner_id",
        "image_url",
        "imageUrl",
        "images",
        "tags",
        "created_at",
    }
)
PUBLIC_VARIANT_RESPONSE_FIELDS = frozenset(
    {
        "id",
        "product_id",
        "sku",
        "size",
        "color",
        "color_hex",
        "price",
        "original_price",
        "stock_quantity",
        "image_url",
        "images",
        "is_active",
        "sort_order",
        "created_at",
    }
)
PUBLIC_STOREFRONT_RESPONSE_FIELDS = frozenset(
    {
        "id",
        "name",
        "name_en",
        "description",
        "logo_url",
        "cover_url",
        "city",
        "category",
        "created_at",
    }
)
PROTECTED_FINANCIAL_FIELDS = frozenset(
    {
        "subtotal",
        "total",
        "amount",
        "discount_amount",
        "discount_total",
        "coupon_discount",
        "points_discount",
        "tax",
        "shipping_cost",
        "shipping_total",
        "paid_amount",
        "remaining_balance",
        "available_balance",
        "pending_balance",
        "withdrawn_balance",
        "total_earned",
        "total_spent",
        "total_points",
        "available_points",
        "balance",
        "commission_amount",
        "commission_rate",
        "partner_amount",
        "currency_code",
    }
)
PROTECTED_STATUS_FIELDS = frozenset(
    {
        "status",
        "payment_status",
        "approval_status",
        "is_approved",
        "is_rejected",
        "is_deleted",
        "is_hidden",
        "current_uses",
        "orders_settled",
        "tier_id",
    }
)

ORDER_PROTECTED_FIELDS = frozenset(
    {
        "status",
        "payment_status",
        "subtotal",
        "total",
        "discount_amount",
        "discount_total",
        "coupon_discount",
        "points_discount",
        "tax",
        "shipping_cost",
        "shipping_total",
        "paid_amount",
        "remaining_balance",
        "payment_method",
        "currency_code",
        "user_id",
        "partner_id",
        "merchant_id",
        "order_number",
        "shipping_address",
        "billing_address",
        "approval_status",
        "approval_notes",
        "internal_notes",
        "admin_notes",
        "deleted_at",
        "is_deleted",
        "is_hidden",
        "extra_data",
    }
)

GENERIC_WRITE_BLOCKED_FOR_NON_STAFF = frozenset(
    {
        "orders",
        "order_items",
        "order_status_history",
        "order_payments",
        "payments",
        "payment_receipts",
        "refunds",
        "returns",
        "order_financials",
        "financial_vouchers",
        "cash_transactions",
        "employee_payments",
        "general_expenses",
        "partner_wallets",
        "partner_settlements",
        "partner_payments",
        "partner_contracts",
        "marketer_commissions",
        "marketer_payments",
        "user_loyalty",
        "points_transactions",
        "coupon_usage",
    }
)

GENERIC_DELETE_ALLOWED_FOR_NON_STAFF = frozenset(
    {
        "wishlist",
        "user_cart",
        "product_likes",
        "product_comparisons",
        "customer_addresses",
    }
)

NON_STAFF_INSERT_FIELDS: dict[str, frozenset[str]] = {
    "profiles": frozenset({"full_name", "email", "phone", "city", "avatar_url", "classification", "store_name", "store_logo_url", "store_description", "user_id"}),
    "customer_addresses": frozenset({"user_id", "label", "recipient_name", "phone", "governorate", "city", "address", "latitude", "longitude", "is_default"}),
    "wishlist": frozenset({"user_id", "product_id"}),
    "user_cart": frozenset({"user_id", "product_id", "variant_id", "quantity"}),
    "product_likes": frozenset({"user_id", "product_id"}),
    "product_comparisons": frozenset({"user_id", "product_id", "product_ids"}),
    "product_reviews": frozenset({"user_id", "product_id", "title", "body", "rating", "comment", "status"}),
    "store_reviews": frozenset({"user_id", "partner_id", "title", "body", "rating", "comment", "status"}),
    "support_tickets": frozenset({"user_id", "subject", "description", "status"}),
    "ticket_messages": frozenset({"ticket_id", "sender_id", "message"}),
    "account_deletion_requests": frozenset({"user_id", "reason", "status"}),
    "contact_messages": frozenset({"user_id", "name", "email", "phone", "subject", "message", "status"}),
    "notification_preferences": frozenset({"user_id", "in_app_enabled", "mobile_push_enabled", "web_push_enabled", "order_updates", "payment_updates", "shipping_updates", "promotional_notifications", "support_updates", "security_notifications", "system_notifications", "status"}),
    "courier_location_updates": frozenset({"courier_id", "user_id", "assignment_id", "latitude", "longitude"}),
    "products": frozenset({"name", "name_en", "sku", "short_code", "description", "rich_description", "price", "original_price", "currency_code", "stock_quantity", "track_inventory", "category_id", "brand_id", "image_url", "images", "tags", "meta_title", "meta_description", "promotional_title"}),
    "product_variants": frozenset({"product_id", "sku", "size", "color", "color_hex", "price", "original_price", "stock_quantity", "image_url", "images", "is_active", "sort_order"}),
    "partner_coupons": frozenset({"code", "title", "amount", "is_active", "expires_at", "partner_id"}),
    "partner_notification_preferences": frozenset({"partner_id", "status", "is_active"}),
    "partner_storefronts": frozenset({"user_id", "partner_id", "name", "email", "phone", "description", "logo_url", "is_active"}),
    "partner_profiles": frozenset({"user_id", "partner_id", "name", "email", "phone", "logo_url"}),
    "public_marketer_codes": frozenset({"user_id", "code", "status", "is_active"}),
}

NON_STAFF_UPDATE_FIELDS: dict[str, frozenset[str]] = {
    "profiles": NON_STAFF_INSERT_FIELDS["profiles"] - frozenset({"user_id"}),
    "customer_addresses": NON_STAFF_INSERT_FIELDS["customer_addresses"] - frozenset({"user_id"}),
    "wishlist": frozenset(),
    "user_cart": frozenset({"quantity"}),
    "product_likes": frozenset(),
    "product_comparisons": frozenset({"product_id", "product_ids"}),
    "product_reviews": frozenset({"title", "body", "rating", "comment"}),
    "store_reviews": frozenset({"title", "body", "rating", "comment"}),
    "support_tickets": frozenset({"subject", "description", "status"}),
    "ticket_messages": frozenset({"message"}),
    "account_deletion_requests": frozenset({"reason"}),
    "notification_preferences": NON_STAFF_INSERT_FIELDS["notification_preferences"] - frozenset({"user_id"}),
    "products": NON_STAFF_INSERT_FIELDS["products"] - frozenset({"sku"}),
    "product_variants": NON_STAFF_INSERT_FIELDS["product_variants"] - frozenset({"product_id", "sku"}),
    "partner_coupons": frozenset({"code", "title", "amount", "is_active", "expires_at"}),
    "partner_notification_preferences": frozenset({"status", "is_active"}),
    "partner_storefronts": frozenset({"name", "email", "phone", "description", "logo_url", "is_active"}),
    "partner_profiles": frozenset({"name", "email", "phone", "logo_url"}),
}

ALLOWED_UPSERT_CONFLICTS: dict[str, frozenset[tuple[str, ...]]] = {
    "profiles": frozenset({("user_id",)}),
    "wishlist": frozenset({("user_id", "product_id")}),
    "user_cart": frozenset({("user_id", "product_id", "variant_id"), ("user_id", "product_id")}),
    "product_likes": frozenset({("user_id", "product_id")}),
    "product_comparisons": frozenset({("user_id", "product_id")}),
    "customer_addresses": frozenset({("id",)}),
    "notification_preferences": frozenset({("user_id",)}),
    "partner_notification_preferences": frozenset({("partner_id",)}),
    "partner_storefronts": frozenset({("partner_id",), ("user_id",)}),
    "partner_profiles": frozenset({("partner_id",), ("user_id",)}),
    "partner_coupons": frozenset({("partner_id", "code")}),
    "products": frozenset({("id",), ("sku",)}),
    "product_variants": frozenset({("id",), ("sku",)}),
    "public_marketer_codes": frozenset({("code",)}),
}

SERVER_DEFAULT_INSERT_VALUES: dict[str, dict[str, Any]] = {
    "product_reviews": {"status": "pending"},
    "store_reviews": {"status": "pending"},
    "support_tickets": {"status": "open"},
    "account_deletion_requests": {"status": "pending"},
    "partner_coupons": {"status": "pending"},
    "products": {"approval_status": "pending", "is_active": True, "is_featured": False},
}


@dataclass(frozen=True)
class ResourcePolicySummary:
    resource: str
    write_blocked_for_non_staff: bool
    non_staff_insert_fields: tuple[str, ...]
    non_staff_update_fields: tuple[str, ...]
    allowed_upsert_conflicts: tuple[tuple[str, ...], ...]
    protected_fields: tuple[str, ...]


def is_staff_roles(roles: set[str]) -> bool:
    return bool(set(roles).intersection(STAFF_ROLES))


def is_admin_roles(roles: set[str]) -> bool:
    return bool(set(roles).intersection(ADMIN_ROLES))


def normalize_conflict_target(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        fields = tuple(item.strip() for item in value.split(",") if item.strip())
    elif isinstance(value, (list, tuple)):
        fields = tuple(str(item).strip() for item in value if str(item).strip())
    else:
        raise HTTPException(status_code=422, detail="invalid_on_conflict")
    if not fields:
        raise HTTPException(status_code=422, detail="invalid_on_conflict")
    return fields


def parse_uuidish(value: Any, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid_owner_uuid:{field}")


def assert_owner_value(field: str, value: Any, current_user_id: uuid.UUID | None) -> None:
    if current_user_id is None:
        raise HTTPException(status_code=401, detail="authentication_required")
    parsed = parse_uuidish(value, field)
    if parsed is not None and parsed != current_user_id:
        raise HTTPException(status_code=403, detail=f"owner_field_mismatch:{field}")


def protected_fields_for(table: str) -> frozenset[str]:
    protected = SECRET_FIELDS | AUDIT_FIELDS
    if table == "orders":
        protected |= ORDER_PROTECTED_FIELDS
    if table in {
        "orders",
        "order_items",
        "order_payments",
        "payments",
        "payment_receipts",
        "refunds",
        "partner_wallets",
        "partner_settlements",
        "partner_payments",
        "marketer_commissions",
        "marketer_payments",
        "user_loyalty",
        "points_transactions",
        "coupon_usage",
    }:
        protected |= PROTECTED_FINANCIAL_FIELDS | PROTECTED_STATUS_FIELDS | OWNER_FIELDS
    if table in {"product_reviews", "store_reviews"}:
        protected |= frozenset(
            {
                "is_approved",
                "approval_status",
                "is_rejected",
                "rejected_reason",
                "reviewed_by",
                "reviewed_at",
                "moderation_notes",
                "admin_notes",
                "merchant_reply",
            }
        )
    if table in {"products", "product_variants"}:
        protected |= frozenset(
            {
                "partner_id",
                "approval_status",
                "approval_notes",
                "approved_at",
                "approved_by",
                "is_featured",
                "featured_at",
                "featured_until",
                "is_promoted",
                "promotional",
                "promotion_priority",
                "promotion_type",
                "homepage_priority",
                "sponsored",
                "featured_rank",
                "is_active",
                "supplier_cost",
                "cost_price",
                "min_stock_quantity",
            }
        )
    if table in {"partner_contracts", "partner_coupons"}:
        protected |= frozenset(
            {
                "partner_id",
                "commission_rate",
                "current_uses",
                "approved_by",
                "signed_by_admin",
                "financial_terms",
                "settlement_terms",
            }
        )
    return frozenset(protected)


def validate_resource_operation(table: str, operation: str, roles: set[str]) -> None:
    if operation not in {"select", "insert", "update", "upsert", "delete"}:
        raise HTTPException(status_code=400, detail="unsupported_resource_operation")
    if operation == "select" and table in GENERIC_MESSAGING_SELECT_BLOCKED:
        raise HTTPException(status_code=403, detail=f"generic_messaging_resource_bypass_denied:{table}")
    if operation in MUTATING_OPERATIONS and table in GENERIC_MESSAGING_MUTATION_BLOCKED:
        raise HTTPException(status_code=403, detail=f"generic_messaging_resource_bypass_denied:{table}")
    if operation == "select" or is_staff_roles(roles):
        return
    if operation == "delete" and table not in GENERIC_DELETE_ALLOWED_FOR_NON_STAFF:
        raise HTTPException(status_code=403, detail=f"resource_delete_policy_denied:{table}")
    if table in GENERIC_WRITE_BLOCKED_FOR_NON_STAFF:
        raise HTTPException(status_code=403, detail=f"resource_write_policy_denied:{table}")
    if operation in {"insert", "upsert"} and table not in NON_STAFF_INSERT_FIELDS:
        raise HTTPException(status_code=403, detail=f"resource_insert_policy_denied:{table}")
    if operation == "update" and table not in NON_STAFF_UPDATE_FIELDS:
        raise HTTPException(status_code=403, detail=f"resource_update_policy_denied:{table}")


def validate_conflict_target(table: str, target: tuple[str, ...] | None, roles: set[str]) -> tuple[str, ...] | None:
    if target is None:
        return None
    if is_staff_roles(roles):
        return target
    allowed = ALLOWED_UPSERT_CONFLICTS.get(table, frozenset())
    if target not in allowed:
        raise HTTPException(status_code=403, detail=f"on_conflict_policy_denied:{table}:{','.join(target)}")
    protected = protected_fields_for(table)
    if any(field in protected for field in target if field != "id"):
        raise HTTPException(status_code=403, detail=f"on_conflict_protected_field:{table}")
    return target


def validate_mutation_payload(
    *,
    table: str,
    operation: str,
    raw: dict[str, Any],
    table_columns: set[str],
    roles: set[str],
    current_user_id: uuid.UUID | None,
) -> None:
    if is_staff_roles(roles):
        return
    validate_resource_operation(table, operation, roles)
    if operation == "delete":
        return
    allowed = NON_STAFF_INSERT_FIELDS.get(table, frozenset()) if operation in {"insert", "upsert"} else NON_STAFF_UPDATE_FIELDS.get(table, frozenset())
    protected = protected_fields_for(table)
    for key, value in raw.items():
        if key == "extra_data":
            raise HTTPException(status_code=403, detail=f"protected_mutation_field:{table}:extra_data")
        if key in OWNER_FIELDS:
            assert_owner_value(key, value, current_user_id)
            continue
        if key in protected:
            allowed_pending_status = table in {"product_reviews", "store_reviews"} and key == "status" and operation in {"insert", "upsert"} and str(value or "").lower() in {"", "pending"}
            if not allowed_pending_status:
                raise HTTPException(status_code=403, detail=f"protected_mutation_field:{table}:{key}")
        if key == "id" and operation in {"insert", "upsert"}:
            continue
        if key not in allowed:
            if key not in table_columns:
                raise HTTPException(status_code=422, detail=f"unknown_mutation_field:{table}:{key}")
            raise HTTPException(status_code=422, detail=f"field_not_mutable:{table}:{key}")


def server_default_values(table: str) -> dict[str, Any]:
    return dict(SERVER_DEFAULT_INSERT_VALUES.get(table, {}))


def allowed_select_columns(table: str, requested: list[str] | None, table_columns: set[str], roles: set[str]) -> list[str] | None:
    if requested is None:
        return None
    protected = INTERNAL_RESPONSE_FIELDS if not is_staff_roles(roles) else SECRET_FIELDS
    clean: list[str] = []
    for column in requested:
        if column in protected or column == "extra_data":
            raise HTTPException(status_code=403, detail=f"select_field_denied:{table}:{column}")
        if column not in table_columns:
            raise HTTPException(status_code=422, detail=f"unknown_select_field:{table}:{column}")
        if column not in clean:
            clean.append(column)
    return clean


def response_field_allowed(table: str, field: str, roles: set[str], selected: set[str] | None) -> bool:
    if selected is not None and field not in selected:
        return False
    if field in SECRET_FIELDS:
        return False
    if is_staff_roles(roles):
        return True
    if field in INTERNAL_RESPONSE_FIELDS:
        return False
    if table == "products" and "partner" not in roles and field not in PUBLIC_PRODUCT_RESPONSE_FIELDS:
        return False
    if table == "product_variants" and "partner" not in roles and field not in PUBLIC_VARIANT_RESPONSE_FIELDS:
        return False
    if table == "partner_storefronts" and "partner" not in roles and field not in PUBLIC_STOREFRONT_RESPONSE_FIELDS:
        return False
    if table in {"products", "product_variants"} and field in protected_fields_for(table):
        return False
    if table in {"partner_wallets", "partner_settlements", "partner_payments", "marketer_commissions", "marketer_payments"} and field in PROTECTED_FINANCIAL_FIELDS:
        return False
    return True


def registry_snapshot() -> list[dict[str, Any]]:
    resources = sorted(
        set(GENERIC_WRITE_BLOCKED_FOR_NON_STAFF)
        | set(NON_STAFF_INSERT_FIELDS)
        | set(NON_STAFF_UPDATE_FIELDS)
        | set(ALLOWED_UPSERT_CONFLICTS)
    )
    rows: list[dict[str, Any]] = []
    for resource in resources:
        summary = ResourcePolicySummary(
            resource=resource,
            write_blocked_for_non_staff=resource in GENERIC_WRITE_BLOCKED_FOR_NON_STAFF,
            non_staff_insert_fields=tuple(sorted(NON_STAFF_INSERT_FIELDS.get(resource, frozenset()))),
            non_staff_update_fields=tuple(sorted(NON_STAFF_UPDATE_FIELDS.get(resource, frozenset()))),
            allowed_upsert_conflicts=tuple(sorted(ALLOWED_UPSERT_CONFLICTS.get(resource, frozenset()))),
            protected_fields=tuple(sorted(protected_fields_for(resource))),
        )
        rows.append(asdict(summary))
    return rows
