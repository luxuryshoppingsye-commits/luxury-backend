from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import DateTime, String, and_, cast, false, func, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect

from ..config import get_settings
from ..models import MODEL_BY_TABLE, RESOURCE_TABLES
from ..services.resource_policy import (
    INTERNAL_RESPONSE_FIELDS,
    allowed_select_columns,
    normalize_conflict_target,
    response_field_allowed,
    server_default_values,
    validate_conflict_target,
    validate_mutation_payload,
    validate_resource_operation,
)
from ..services.catalog_policy import public_product_clauses


COMPATIBLE_COLUMN_ALIASES = {
    "site_settings": {
        # The Flutter admin client uses the public setting contract while the
        # PostgreSQL compatibility table stores the key in ``name``.
        "setting_key": "name",
    },
    "static_pages": {
        "content": "body",
        "is_published": "is_active",
    },
    "page_sections": {
        "content": "body",
        "is_visible": "is_active",
        "section_name": "title",
    },
    "custom_elements": {
        "element_type": "type",
        "title": "name",
        "content": "body",
        "is_visible": "is_active",
    },
    "banners": {
        "link_url": "url",
    },
    "blog_articles": {
        "content": "body",
        "cover_image": "image_url",
        "is_published": "is_active",
        "published_at": "created_at",
    },
}
COMPATIBLE_RESPONSE_ALIASES = {
    table: {alias: column for alias, column in aliases.items() if alias != "published_at"}
    for table, aliases in COMPATIBLE_COLUMN_ALIASES.items()
}
PUBLIC_READ_TABLES = {
    "categories", "brands", "products", "product_variants", "banners",
    "partner_storefronts", "local_merchants", "store_reviews", "product_reviews",
    "global_sites", "currencies", "site_content", "site_menus", "site_settings",
    "theme_settings", "social_links", "static_pages", "page_sections",
    "blog_articles", "custom_elements", "public_marketer_codes", "shipping_zones",
    "shipping_carriers", "shipping_stages", "loyalty_tiers",
}
USER_OWNED_TABLES = {
    "profiles", "wishlist", "user_cart", "notifications", "orders",
    "support_tickets", "account_deletion_requests", "user_loyalty",
    "points_transactions", "product_likes", "product_comparisons",
    "product_reviews", "store_reviews", "customer_addresses",
}
PARTNER_OWNED_TABLES = {
    "partner_profiles", "partner_storefronts", "partner_wallets", "partner_contracts",
    "partner_coupons", "partner_notification_preferences", "partner_order_items",
    "partner_order_requests", "partner_payments", "partner_settlements",
}
MERCHANT_TYPED_ORDER_ENDPOINT_TABLES = {
    "orders",
    "order_items",
    "order_status_history",
    "order_payments",
    "payments",
    "payment_receipts",
    "refunds",
    "returns",
    "order_financials",
    "order_shipping",
    "shipping_history",
    "courier_assignments",
}
ADMIN_ROLES = {"admin", "manager"}
STAFF_ROLES = ADMIN_ROLES | {"finance", "logistics", "staff", "employee"}
FINANCE_TABLES = {
    "orders", "order_items", "order_payments", "payments", "payment_receipts",
    "refunds", "employee_payments", "general_expenses", "marketer_commissions",
    "marketer_payments", "partner_payments", "partner_settlements", "order_financials",
    "financial_vouchers", "cash_transactions", "financial_reports", "vouchers",
}
FINANCE_REFERENCE_TABLES = {
    "marketers", "partner_applications", "partner_profiles",
    "partner_storefronts", "local_merchants",
}
LOGISTICS_TABLES = {
    "orders", "order_items", "order_shipping", "shipping_history", "shipping_zones",
    "shipping_carriers", "shipping_stages", "couriers", "courier_assignments",
    "courier_location_updates", "warehouses", "inventory", "inventory_movements",
    "inventory_locations", "order_status_history",
}
SUPPORT_TABLES = {"support_tickets", "ticket_messages"}


def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _repair_mojibake(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not any(marker in value for marker in ("\u00d8", "\u00d9", "\u00c3", "\u00c2", "\ufffd")):
        return value
    try:
        repaired = value.encode("latin1", errors="strict").decode("utf-8")
    except UnicodeError:
        return value
    return repaired if repaired else value


def serialize_record(record: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    table_name = record.__table__.name
    object_state = inspect(record)
    expired = object_state.expired_attributes
    for attribute in inspect(record.__class__).column_attrs:
        key = attribute.key
        if key in expired:
            continue
        value = object_state.dict.get(key)
        if key == "extra_data":
            if isinstance(value, dict):
                if table_name == "site_settings":
                    result["setting_value"] = _json_value(value)
                for extra_key, extra_value in value.items():
                    result.setdefault(extra_key, _json_value(extra_value))
            continue
        result[key] = _json_value(value)
    if table_name == "site_settings" and "name" in result:
        result.setdefault("setting_key", result["name"])
    if "image_url" in result and result.get("image_url"):
        result.setdefault("imageUrl", result["image_url"])
    for alias, column_name in COMPATIBLE_RESPONSE_ALIASES.get(table_name, {}).items():
        if column_name in result:
            result.setdefault(alias, result[column_name])
    if table_name == "blog_articles" and "created_at" in result:
        result.setdefault("published_at", result["created_at"])
    return result


def _column_value(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, UUID):
        try:
            return uuid.UUID(str(value))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid_uuid:{column.name}")
    if isinstance(column.type, DateTime) and isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid_datetime:{column.name}")
    if isinstance(column.type, JSONB):
        return value
    if isinstance(value, str):
        return _repair_mojibake(value)
    return value


def _json_text_filter_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_json_text_filter_value(item) for item in value]
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _split_projection(value: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_select_columns(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "*":
        return None
    columns: list[str] = []
    for part in _split_projection(text):
        if not part or part == "*":
            continue
        if "(" in part or ")" in part or "!" in part:
            continue
        column_name = part.split(":", 1)[-1].strip()
        if column_name and column_name not in columns:
            columns.append(column_name)
    return columns or None


def _parse_is_value(value: Any) -> Any:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"null", "none"}:
            return None
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return value


def _normalize_resource_payload(
    table: str,
    raw: dict[str, Any],
    operation: str,
) -> dict[str, Any]:
    """Normalize dashboard aliases before the generic resource writer persists them."""
    values = dict(raw)
    if table == "site_settings":
        # Keep both the REST content API and the resource API on the same
        # storage contract. Older clients send setting_key/setting_value,
        # while this compatibility schema stores the key in name and the
        # JSON value in extra_data.
        if "setting_key" in values and "name" not in values:
            values["name"] = values.pop("setting_key")
        elif "key" in values and "name" not in values:
            values["name"] = values.pop("key")
        if "setting_value" in values:
            setting_value = values.pop("setting_value")
            values["extra_data"] = (
                dict(setting_value)
                if isinstance(setting_value, dict)
                else {"value": setting_value}
            )
        elif "value" in values and "extra_data" not in values:
            values["extra_data"] = {"value": values.pop("value")}
        return values
    aliases = COMPATIBLE_COLUMN_ALIASES.get(table, {})
    if table not in {"site_settings", "couriers"}:
        for alias, column_name in aliases.items():
            if alias in values and column_name not in values:
                values[column_name] = values.pop(alias)
        return values

    if "full_name" in values and "name" not in values:
        values["name"] = values["full_name"]
    if "isActive" in values and "is_active" not in values:
        values["is_active"] = values["isActive"]
    if "vehicleType" in values and "vehicle_type" not in values:
        values["vehicle_type"] = values["vehicleType"]
    if "coverageArea" in values and "coverage_area" not in values:
        values["coverage_area"] = values["coverageArea"]

    if operation in {"insert", "upsert"} or "name" in values:
        name = str(values.get("name") or "").strip()
        if len(name) < 2:
            raise HTTPException(status_code=422, detail="courier_name_required")
        values["name"] = name
    if operation in {"insert", "upsert"}:
        active = values.get("is_active", True)
        if isinstance(active, str):
            active = active.strip().lower() not in {"false", "0", "no", "inactive"}
        values.setdefault("status", "active" if active else "inactive")
    elif "is_active" in values and "status" not in values:
        active = values["is_active"]
        if isinstance(active, str):
            active = active.strip().lower() not in {"false", "0", "no", "inactive"}
        values["status"] = "active" if active else "inactive"
    return values


class ResourceRepository:
    def __init__(self, session: AsyncSession, table: str, user_id: uuid.UUID | None, roles: set[str]):
        if table not in RESOURCE_TABLES:
            raise HTTPException(status_code=404, detail="resource_not_found")
        self.session = session
        self.table = table
        self.model = MODEL_BY_TABLE[table]
        self.user_id = user_id
        self.roles = roles

    @property
    def is_staff(self) -> bool:
        return bool(self.roles.intersection(STAFF_ROLES))

    @property
    def is_admin(self) -> bool:
        return bool(self.roles.intersection(ADMIN_ROLES))

    def _staff_table_allowed(self, operation: str) -> bool:
        if self.is_admin:
            return True
        if "finance" in self.roles and self.table in FINANCE_TABLES:
            return True
        if "finance" in self.roles and operation == "select" and self.table in FINANCE_REFERENCE_TABLES:
            return True
        if "logistics" in self.roles and self.table in LOGISTICS_TABLES:
            return True
        if self.roles.intersection({"staff", "employee"}) and self.table in SUPPORT_TABLES:
            return operation in {"select", "insert", "update"}
        if self.roles.intersection({"staff", "employee"}):
            return operation == "select" and self.table in (SUPPORT_TABLES | {"products", "categories"})
        return False

    def ensure_access(self, operation: str) -> None:
        validate_resource_operation(self.table, operation, self.roles)
        if operation == "select" and self.table in PUBLIC_READ_TABLES:
            return
        if self.user_id is None:
            raise HTTPException(status_code=401, detail="authentication_required")
        if self.is_staff:
            if self._staff_table_allowed(operation):
                return
            raise HTTPException(status_code=403, detail="resource_role_scope_denied")
        if "partner" in self.roles and operation == "select" and self.table in MERCHANT_TYPED_ORDER_ENDPOINT_TABLES:
            raise HTTPException(status_code=403, detail="merchant_typed_endpoint_required")
        if self.table in USER_OWNED_TABLES:
            return
        if "partner" in self.roles and (self.table in PARTNER_OWNED_TABLES or self.table in {"products", "product_variants"}):
            return
        if self.table in {
            "orders",
            "order_shipping",
            "shipping_history",
            "courier_assignments",
            "courier_location_updates",
            "couriers",
            "order_status_history",
        } and self.roles.intersection({"courier", "delivery"}):
            if operation == "select":
                return
            raise HTTPException(status_code=403, detail="courier_generic_mutation_denied")
        if self.table in {"marketers", "marketer_commissions", "marketer_payments", "public_marketer_codes"} and "marketer" in self.roles:
            return
        raise HTTPException(status_code=403, detail="resource_access_denied")

    def _ownership_clause(self):
        table = self.model.__table__
        if self.user_id is None or self.is_staff:
            return None
        if self.roles.intersection({"courier", "delivery"}) and self.table in {
            "orders",
            "order_status_history",
            "order_shipping",
            "shipping_history",
        }:
            assignment_table = MODEL_BY_TABLE["courier_assignments"].__table__
            courier_clauses = []
            for name in ("courier_id", "user_id"):
                if name in assignment_table.c:
                    courier_clauses.append(assignment_table.c[name] == self.user_id)
            if not courier_clauses:
                return false()
            active_statuses = ("active", "assigned", "accepted", "picked_up", "out_for_delivery")
            assignment_query = select(assignment_table.c.order_id).where(or_(*courier_clauses))
            if "status" in assignment_table.c:
                assignment_query = assignment_query.where(assignment_table.c.status.in_(active_statuses))
            if self.table == "orders":
                return table.c.id.in_(assignment_query)
            if "order_id" in table.c:
                return table.c.order_id.in_(assignment_query)
            return false()
        if self.table in USER_OWNED_TABLES:
            if "user_id" in table.c:
                return table.c.user_id == self.user_id
            if self.table == "profiles" and "id" in table.c:
                return table.c.id == self.user_id
        if self.table in PARTNER_OWNED_TABLES:
            candidates = []
            if "user_id" in table.c:
                candidates.append(table.c.user_id == self.user_id)
            if "partner_id" in table.c:
                candidates.append(table.c.partner_id == self.user_id)
            return or_(*candidates) if candidates else None
        if self.table == "products" and "partner" in self.roles:
            return table.c.partner_id == self.user_id
        if self.table == "product_variants" and "partner" in self.roles:
            product_table = MODEL_BY_TABLE["products"].__table__
            return table.c.product_id.in_(
                select(product_table.c.id).where(
                    product_table.c.partner_id == self.user_id,
                    product_table.c.deleted_at.is_(None),
                )
            )
        if self.table in {"courier_assignments", "courier_location_updates", "couriers"}:
            candidates = []
            for name in ("user_id", "courier_id"):
                if name in table.c:
                    candidates.append(table.c[name] == self.user_id)
            return or_(*candidates) if candidates else None
        if (self.table.startswith("marketer") or self.table == "public_marketer_codes") and "user_id" in table.c:
            return table.c.user_id == self.user_id
        return None

    def _public_visibility_clauses(self) -> list[Any]:
        if self.is_staff:
            return []
        table = self.model.__table__
        if self.table == "products" and "partner" not in self.roles:
            return public_product_clauses(self.model)
        if self.table == "product_variants" and "partner" not in self.roles:
            product_table = MODEL_BY_TABLE["products"]
            return [
                table.c.deleted_at.is_(None),
                table.c.is_active.is_(True),
                table.c.product_id.in_(
                    select(product_table.id).where(*public_product_clauses(product_table))
                ),
            ]
        if self.table == "partner_storefronts" and "partner" not in self.roles:
            clauses = []
            if "deleted_at" in table.c:
                clauses.append(table.c.deleted_at.is_(None))
            if "is_active" in table.c:
                clauses.append(table.c.is_active.is_(True))
            return clauses
        return []

    def _filter_clause(self, item: dict[str, Any]):
        raw_column_name = str(item.get("column") or "")
        column_name = COMPATIBLE_COLUMN_ALIASES.get(self.table, {}).get(
            raw_column_name,
            raw_column_name,
        )
        operator = str(item.get("operator") or "eq").replace(".", "_")
        value = item.get("value")
        table = self.model.__table__
        if column_name == "_or":
            return self._or_filter_clause(str(value or ""))
        if "." in column_name:
            return self._relation_filter_clause(column_name, operator, value)
        if (
            not self.is_staff
            and column_name in INTERNAL_RESPONSE_FIELDS
            and not (
                column_name == "deleted_at"
                and operator == "is"
                and value is None
                and column_name in table.c
            )
        ):
            raise HTTPException(status_code=403, detail=f"filter_field_denied:{self.table}:{column_name}")
        if column_name in table.c:
            column = table.c[column_name]
            base_operator = operator[4:] if operator.startswith("not_") else operator
            if base_operator != "is":
                if isinstance(value, list):
                    value = [_column_value(column, item) for item in value]
                else:
                    value = _column_value(column, value)
        elif "extra_data" in table.c:
            if not self.is_staff and column_name in INTERNAL_RESPONSE_FIELDS:
                raise HTTPException(status_code=403, detail=f"filter_field_denied:{self.table}:{column_name}")
            column = table.c.extra_data[column_name].astext
            value = _json_text_filter_value(value)
        else:
            raise HTTPException(status_code=400, detail=f"unknown_filter_column:{column_name}")
        return self._operator_clause(column, operator, value)

    def _operator_clause(self, column, operator: str, value: Any):
        negated = operator.startswith("not_")
        base_operator = operator[4:] if negated else operator
        if base_operator == "eq":
            clause = column == value
        elif base_operator == "neq":
            clause = column != value
        elif base_operator == "gt":
            clause = column > value
        elif base_operator == "gte":
            clause = column >= value
        elif base_operator == "lt":
            clause = column < value
        elif base_operator == "lte":
            clause = column <= value
        elif base_operator in {"like", "ilike"}:
            if not self.is_staff:
                pattern = str(value or "")
                if len(pattern) > get_settings().search_max_query_length:
                    raise HTTPException(status_code=422, detail="search_query_too_long")
                if not pattern.replace("%", "").replace("_", "").strip():
                    raise HTTPException(status_code=422, detail="unbounded_wildcard_filter_denied")
            clause = getattr(cast(column, String), base_operator)(str(value))
        elif base_operator == "in":
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1]
                value = [item.strip() for item in text.split(",") if item.strip()]
            if not isinstance(value, list):
                raise HTTPException(status_code=422, detail=f"invalid_filter_value:{base_operator}")
            if not value:
                clause = false()
            else:
                clause = column.in_(value)
        elif base_operator == "is":
            parsed = _parse_is_value(value)
            if parsed not in {None, True, False}:
                raise HTTPException(status_code=422, detail="invalid_is_filter_value")
            clause = column.is_(parsed)
        elif base_operator == "contains":
            clause = column.contains(value)
        else:
            raise HTTPException(status_code=400, detail=f"unsupported_filter:{operator}")
        if negated:
            if base_operator == "in" and isinstance(value, list) and not value:
                return true()
            return ~clause
        return clause

    def _or_filter_clause(self, expression: str):
        clauses = []
        parts = [part for part in expression.split(",") if part.strip()]
        if len(parts) > get_settings().resource_max_filters:
            raise HTTPException(status_code=422, detail="too_many_or_filters")
        for raw_part in parts:
            part = raw_part.strip()
            if not part:
                continue
            first, separator, remainder = part.partition(".")
            if not separator or not remainder:
                raise HTTPException(status_code=422, detail="invalid_or_filter")
            if remainder.startswith("not."):
                second, _, value = remainder[4:].partition(".")
                operator = f"not_{second}"
            else:
                operator, _, value = remainder.partition(".")
            if not operator:
                raise HTTPException(status_code=422, detail="invalid_or_filter")
            clauses.append(self._filter_clause({"column": first, "operator": operator, "value": value}))
        return or_(*clauses) if clauses else None

    def _relation_filter_clause(self, column_name: str, operator: str, value: Any):
        if self.table == "order_items" and column_name == "products.partner_id":
            product_table = MODEL_BY_TABLE["products"].__table__
            product_column = product_table.c.partner_id
            value = _column_value(product_column, value)
            return self.model.__table__.c.product_id.in_(
                select(product_table.c.id).where(self._operator_clause(product_column, operator, value))
            )
        raise HTTPException(status_code=422, detail=f"unsupported_relation_filter:{column_name}")

    def _serialize_response(self, record: Any, selected_columns: set[str] | None = None) -> dict[str, Any]:
        row = serialize_record(record)
        return {
            key: value
            for key, value in row.items()
            if response_field_allowed(self.table, key, self.roles, selected_columns)
        }

    async def select(self, payload: dict[str, Any]) -> Any:
        self.ensure_access("select")
        filters = payload.get("filters") or []
        if not isinstance(filters, list):
            raise HTTPException(status_code=422, detail="invalid_resource_filters")
        if len(filters) > get_settings().resource_max_filters:
            raise HTTPException(status_code=422, detail="too_many_resource_filters")
        raw_selected_columns = _parse_select_columns(payload.get("columns"))
        resolved_selected_columns = None
        if raw_selected_columns is not None:
            resolved = [
                COMPATIBLE_COLUMN_ALIASES.get(self.table, {}).get(column, column)
                for column in raw_selected_columns
            ]
            resolved_selected_columns = allowed_select_columns(
                self.table,
                resolved,
                set(self.model.__table__.c.keys()),
                self.roles,
            )
        selected_response_fields: set[str] | None = None
        if resolved_selected_columns is not None:
            selected_response_fields = set(resolved_selected_columns)
            selected_response_fields.update(raw_selected_columns or [])

        statement = select(self.model)
        clauses = [clause for clause in [self._ownership_clause()] if clause is not None]
        clauses.extend(self._public_visibility_clauses())
        for item in filters:
            clause = self._filter_clause(item)
            if clause is not None:
                clauses.append(clause)
        if "deleted_at" in self.model.__table__.c:
            clauses.append(self.model.__table__.c.deleted_at.is_(None))
        if clauses:
            statement = statement.where(and_(*clauses))
        count_statement = select(func.count()).select_from(self.model)
        if clauses:
            count_statement = count_statement.where(and_(*clauses))
        order_name = payload.get("order")
        if order_name:
            table = self.model.__table__
            requested_order = str(order_name)
            resolved_order = COMPATIBLE_COLUMN_ALIASES.get(self.table, {}).get(
                requested_order,
                requested_order,
            )
            column = table.c.get(resolved_order)
            if column is not None:
                statement = statement.order_by(column.asc() if payload.get("ascending", True) else column.desc())
            else:
                raise HTTPException(status_code=422, detail=f"unknown_order_column:{requested_order}")
        settings = get_settings()
        maximum_limit = settings.resource_admin_max_page_size if self.is_staff else settings.resource_max_page_size
        limit = min(max(int(payload.get("limit") or maximum_limit), 1), maximum_limit)
        offset = max(int(payload.get("offset") or 0), 0)
        single = bool(payload.get("single"))
        maybe_single = bool(payload.get("maybeSingle") or payload.get("maybe_single"))
        include_count = bool(payload.get("count"))
        if single or maybe_single:
            statement = statement.offset(offset).limit(2)
        else:
            statement = statement.offset(offset).limit(limit)
        total: int | None = None
        if include_count:
            total = int((await self.session.execute(count_statement)).scalar_one() or 0)
        result = await self.session.execute(statement)
        rows = [self._serialize_response(row, selected_response_fields) for row in result.scalars()]
        if single or maybe_single:
            if not rows:
                if maybe_single:
                    return None
                raise HTTPException(status_code=404, detail="NO_ROWS")
            if len(rows) > 1:
                raise HTTPException(status_code=409, detail="MULTIPLE_ROWS")
            return rows[0]
        if include_count:
            total = total if total is not None else len(rows)
            total_pages = (total + limit - 1) // limit if limit else 0
            page = (offset // limit) + 1 if limit else 1
            return {
                "items": rows,
                "count": total,
                "total": total,
                "page": page,
                "page_size": limit,
                "total_pages": total_pages,
                "has_next": offset + limit < total,
                "has_previous": offset > 0,
            }
        return rows

    def _prepare_data(self, raw: dict[str, Any], operation: str) -> dict[str, Any]:
        raw = _normalize_resource_payload(self.table, raw, operation)
        table = self.model.__table__
        validate_mutation_payload(
            table=self.table,
            operation=operation,
            raw=raw,
            table_columns=set(table.c.keys()),
            roles=self.roles,
            current_user_id=self.user_id,
        )
        data: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in raw.items():
            if key in table.c and key != "extra_data":
                data[key] = _column_value(table.c[key], value)
            else:
                if not self.is_staff:
                    raise HTTPException(status_code=422, detail=f"unknown_mutation_field:{self.table}:{key}")
                extra[key] = value
        if self.user_id is not None and not self.is_staff:
            if self.table in USER_OWNED_TABLES and "user_id" in table.c:
                data["user_id"] = self.user_id
            if self.table in PARTNER_OWNED_TABLES:
                if "partner_id" in table.c:
                    data["partner_id"] = self.user_id
                if "user_id" in table.c:
                    data["user_id"] = self.user_id
            if self.table == "products" and "partner" in self.roles:
                data["partner_id"] = self.user_id
            if "marketer" in self.roles and self.table in {"marketers", "marketer_commissions", "marketer_payments", "public_marketer_codes"} and "user_id" in table.c:
                data["user_id"] = self.user_id
        if not self.is_staff:
            data.update(server_default_values(self.table))
        if "extra_data" in table.c:
            previous = raw.get("extra_data") if isinstance(raw.get("extra_data"), dict) else {}
            data["extra_data"] = {**previous, **extra}
        return data

    async def insert(self, payload: dict[str, Any], upsert: bool = False) -> list[dict[str, Any]]:
        self.ensure_access("upsert" if upsert else "insert")
        conflict_target = validate_conflict_target(
            self.table,
            normalize_conflict_target(payload.get("onConflict")),
            self.roles,
        )
        rows = payload.get("data")
        rows = rows if isinstance(rows, list) else [rows]
        if self.table == "categories":
            return await self._insert_categories(rows, upsert=upsert, conflict_target=conflict_target)
        created = []
        for raw in rows:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="invalid_resource_payload")
            data = self._prepare_data(raw, "upsert" if upsert else "insert")
            await self._verify_parent_ownership(data)
            record = None
            if upsert:
                record = await self._find_upsert_record(data, conflict_target)
            if record is None:
                if upsert and conflict_target is not None:
                    try:
                        async with self.session.begin_nested():
                            record = self.model(**data)
                            self.session.add(record)
                            await self.session.flush()
                    except IntegrityError:
                        record = await self._find_upsert_record(data, conflict_target)
                        if record is None:
                            raise HTTPException(status_code=409, detail="upsert_conflict_retry_failed")
                        if not await self._record_is_accessible(record):
                            raise HTTPException(status_code=404, detail="resource_not_found")
                        if self.table == "product_variants" and data.get("product_id") is not None:
                            current_product_id = getattr(record, "product_id", None)
                            if current_product_id is not None and current_product_id != data["product_id"]:
                                raise HTTPException(status_code=403, detail="variant_product_mismatch")
                        for key, value in data.items():
                            if key != "id" and key not in {"user_id", "owner_id", "partner_id", "merchant_id", "marketer_id", "courier_id"}:
                                setattr(record, key, value)
                        await self.session.flush()
                    created.append(self._serialize_response(record))
                    continue
                record = self.model(**data)
                self.session.add(record)
            else:
                if not await self._record_is_accessible(record):
                    raise HTTPException(status_code=404, detail="resource_not_found")
                if self.table == "product_variants" and data.get("product_id") is not None:
                    current_product_id = getattr(record, "product_id", None)
                    if current_product_id is not None and current_product_id != data["product_id"]:
                        raise HTTPException(status_code=403, detail="variant_product_mismatch")
                for key, value in data.items():
                    if key != "id" and key not in {"user_id", "owner_id", "partner_id", "merchant_id", "marketer_id", "courier_id"}:
                        setattr(record, key, value)
            await self.session.flush()
            created.append(self._serialize_response(record))
        return created

    async def update(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self.ensure_access("update")
        rows = await self.select({**payload, "limit": 2000, "single": False, "maybeSingle": False, "count": False})
        if not rows:
            await self._raise_if_explicit_key_exists_outside_scope(payload)
            return []
        updated = []
        if self.table == "categories":
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise HTTPException(status_code=400, detail="invalid_resource_payload")
            prepared = self._prepare_data(data, "update")
            await self._verify_parent_ownership(prepared)
            for row in rows:
                record = await self._record_from_row(row)
                if record is None:
                    continue
                updated.append(await self._update_category_resource_record(record.id, data))
            return updated
        data = self._prepare_data(payload.get("data") or {}, "update")
        await self._verify_parent_ownership(data)
        for row in rows:
            record = await self._record_from_row(row)
            if record is None:
                continue
            for key, value in data.items():
                if key != "id" and key not in {"user_id", "owner_id", "partner_id", "merchant_id", "marketer_id", "courier_id"}:
                    setattr(record, key, value)
            updated.append(record)
        await self.session.flush()
        return [self._serialize_response(record) for record in updated]

    async def delete(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self.ensure_access("delete")
        rows = await self.select({**payload, "limit": 2000, "single": False, "maybeSingle": False, "count": False})
        if not rows:
            await self._raise_if_explicit_key_exists_outside_scope(payload)
            return []
        deleted_rows = []
        for row in rows:
            record = await self._record_from_row(row)
            if record is None:
                continue
            deleted_rows.append(self._serialize_response(record))
            if self.table == "categories":
                await self._delete_category_resource_record(record.id)
            elif hasattr(record, "deleted_at"):
                record.deleted_at = datetime.now().astimezone()
            else:
                await self.session.delete(record)
        await self.session.flush()
        return deleted_rows

    async def _insert_categories(
        self,
        rows: list[Any],
        *,
        upsert: bool,
        conflict_target: tuple[str, ...] | None,
    ) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="invalid_resource_payload")
            data = self._prepare_data(raw, "upsert" if upsert else "insert")
            await self._verify_parent_ownership(data)
            record = await self._find_upsert_record(data, conflict_target) if upsert else None
            if record is not None:
                if not await self._record_is_accessible(record):
                    raise HTTPException(status_code=404, detail="resource_not_found")
                created.append(await self._update_category_resource_record(record.id, raw))
            else:
                created.append(await self._create_category_resource_record(raw))
        return created

    async def _create_category_resource_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        from ..services.category_integrity import create_category_record

        return await create_category_record(self.session, raw)

    async def _update_category_resource_record(self, record_id: uuid.UUID, raw: dict[str, Any]) -> dict[str, Any]:
        from ..services.category_integrity import update_category_record

        return await update_category_record(self.session, record_id, raw)

    async def _delete_category_resource_record(self, record_id: uuid.UUID) -> None:
        from ..services.category_integrity import soft_delete_category_record

        await soft_delete_category_record(self.session, record_id)

    async def _find_upsert_record(self, data: dict[str, Any], conflict_target: tuple[str, ...] | None):
        table = self.model.__table__
        if conflict_target is not None:
            clauses = []
            for field in conflict_target:
                resolved_field = COMPATIBLE_COLUMN_ALIASES.get(self.table, {}).get(field, field)
                if resolved_field not in table.c:
                    raise HTTPException(status_code=422, detail=f"unknown_on_conflict_field:{self.table}:{field}")
                if resolved_field not in data:
                    raise HTTPException(status_code=422, detail=f"missing_on_conflict_value:{self.table}:{field}")
                value = data[resolved_field]
                clauses.append(table.c[resolved_field].is_(None) if value is None else table.c[resolved_field] == value)
            statement = select(self.model).where(and_(*clauses)).limit(1)
            result = await self.session.execute(statement)
            record = result.scalars().first()
            if record is not None and not await self._record_is_accessible(record):
                raise HTTPException(status_code=404, detail="resource_not_found")
            return record
        if data.get("id") is None:
            return None
        record = await self.session.get(self.model, data["id"])
        if record is not None and not await self._record_is_accessible(record):
            raise HTTPException(status_code=404, detail="resource_not_found")
        return record

    async def _record_is_accessible(self, record: Any) -> bool:
        if self.is_staff:
            return True
        primary_key = list(self.model.__table__.primary_key.columns)
        if not primary_key:
            return False
        clauses = []
        for column in primary_key:
            clauses.append(column == getattr(record, column.name))
        ownership = self._ownership_clause()
        if ownership is None:
            return False
        statement = select(primary_key[0]).where(and_(*(clauses + [ownership]))).limit(1)
        result = await self.session.execute(statement)
        return result.first() is not None

    def _explicit_primary_key_values(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        filters = payload.get("filters") or []
        if not isinstance(filters, list):
            return None
        table = self.model.__table__
        primary_key = list(table.primary_key.columns)
        if not primary_key:
            return None
        values: dict[str, Any] = {}
        for column in primary_key:
            for item in filters:
                if not isinstance(item, dict):
                    continue
                raw_column = str(item.get("column") or "")
                column_name = COMPATIBLE_COLUMN_ALIASES.get(self.table, {}).get(raw_column, raw_column)
                operator = str(item.get("operator") or "eq").replace(".", "_")
                if column_name == column.name and operator == "eq":
                    values[column.name] = _column_value(column, item.get("value"))
                    break
        if len(values) != len(primary_key):
            return None
        return values

    async def _raise_if_explicit_key_exists_outside_scope(self, payload: dict[str, Any]) -> None:
        if self.is_staff:
            return
        values = self._explicit_primary_key_values(payload)
        if values is None:
            return
        table = self.model.__table__
        clauses = [table.c[name] == value for name, value in values.items()]
        statement = select(next(iter(table.primary_key.columns))).where(and_(*clauses)).limit(1)
        result = await self.session.execute(statement)
        if result.first() is not None:
            raise HTTPException(status_code=404, detail="resource_not_found")

    async def _verify_parent_ownership(self, data: dict[str, Any]) -> None:
        if self.is_staff or self.user_id is None:
            return
        if self.table == "product_variants" and "partner" in self.roles and data.get("product_id") is not None:
            product_table = MODEL_BY_TABLE["products"].__table__
            result = await self.session.execute(
                select(product_table.c.id).where(
                    product_table.c.id == data["product_id"],
                    product_table.c.partner_id == self.user_id,
                    product_table.c.deleted_at.is_(None),
                ).limit(1)
            )
            if result.first() is None:
                raise HTTPException(status_code=403, detail="product_variant_parent_policy_denied")

    async def _record_from_row(self, row: dict[str, Any]):
        primary_key = list(self.model.__table__.primary_key.columns)
        if not primary_key:
            return None
        identity: Any
        if len(primary_key) == 1:
            column = primary_key[0]
            identity = _column_value(column, row.get(column.name))
        else:
            identity = {
                column.name: _column_value(column, row.get(column.name))
                for column in primary_key
            }
        return await self.session.get(self.model, identity)
