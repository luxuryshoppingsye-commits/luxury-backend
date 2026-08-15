from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.domain import Order, OrderItem
from .financial_calculator import money


MERCHANT_FORBIDDEN_FIELDS = frozenset(
    {
        "total",
        "subtotal",
        "discount_total",
        "shipping_total",
        "payment_method",
        "payment_status",
        "shipping_address",
        "billing_address",
        "notes",
        "user_id",
        "customer_id",
        "customer_email",
        "customer_phone",
        "customer_profile",
        "payments",
        "payment_receipts",
        "shipping",
        "shippingHistory",
        "global_status_history",
        "extra_data",
        "admin_notes",
        "internal_notes",
        "approval_notes",
    }
)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _amount(value: Any) -> str:
    return str(money(value or 0))


def _item_payload(item: OrderItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "order_id": str(item.order_id),
        "product_id": str(item.product_id) if item.product_id else None,
        "variant_id": str(item.variant_id) if item.variant_id else None,
        "product_name": item.product_name,
        "product_image": item.product_image,
        "quantity": item.quantity,
        "unit_price": _amount(item.unit_price),
        "total_price": _amount(item.total_price),
        "partner_id": str(item.partner_id) if item.partner_id else None,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _financial_payload(total: Decimal, currency_code: str | None) -> dict[str, Any]:
    safe_total = money(total)
    return {
        "gross_total": str(safe_total),
        "net_total": str(safe_total),
        "merchant_receivable": str(safe_total),
        "currency_code": currency_code or "YER",
        "allocation_basis": "own_order_items_only",
    }


def _order_projection(row: dict[str, Any], items: list[OrderItem]) -> dict[str, Any]:
    total = money(row.get("merchant_total") or sum((money(item.total_price) for item in items), Decimal("0")))
    item_payloads = [_item_payload(item) for item in items]
    currency = row.get("currency_code") or "YER"
    return {
        "id": str(row["id"]),
        "order_id": str(row["id"]),
        "merchant_order_id": f"{row['id']}:{row['partner_id']}",
        "order_number": row.get("order_number"),
        "status": row.get("status"),
        "currency_code": currency,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "partner_id": str(row["partner_id"]),
        "items_count": int(row.get("items_count") or len(items)),
        "quantity": int(row.get("quantity") or sum((item.quantity for item in items), 0)),
        "merchant_total": str(total),
        "financial": _financial_payload(total, currency),
        "items": item_payloads,
    }


async def _items_by_order(
    session: AsyncSession,
    *,
    partner_id: uuid.UUID,
    order_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[OrderItem]]:
    if not order_ids:
        return {}
    result = await session.execute(
        select(OrderItem)
        .where(
            OrderItem.order_id.in_(order_ids),
            OrderItem.partner_id == partner_id,
        )
        .order_by(OrderItem.created_at.asc())
    )
    rows: dict[uuid.UUID, list[OrderItem]] = {}
    for item in result.scalars():
        rows.setdefault(item.order_id, []).append(item)
    return rows


async def merchant_order_list(
    session: AsyncSession,
    *,
    partner_id: uuid.UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    statement = (
        select(
            Order.id.label("id"),
            Order.order_number.label("order_number"),
            Order.status.label("status"),
            Order.currency_code.label("currency_code"),
            Order.created_at.label("created_at"),
            Order.updated_at.label("updated_at"),
            OrderItem.partner_id.label("partner_id"),
            func.count(OrderItem.id).label("items_count"),
            func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity"),
            func.coalesce(func.sum(OrderItem.total_price), 0).label("merchant_total"),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.deleted_at.is_(None),
            OrderItem.partner_id == partner_id,
        )
        .group_by(
            Order.id,
            Order.order_number,
            Order.status,
            Order.currency_code,
            Order.created_at,
            Order.updated_at,
            OrderItem.partner_id,
        )
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    rows = [dict(row) for row in (await session.execute(statement)).mappings().all()]
    items = await _items_by_order(session, partner_id=partner_id, order_ids=[row["id"] for row in rows])
    return [_order_projection(row, items.get(row["id"], [])) for row in rows]


async def merchant_order_detail(
    session: AsyncSession,
    *,
    partner_id: uuid.UUID,
    order_id: uuid.UUID,
) -> dict[str, Any]:
    statement = (
        select(
            Order.id.label("id"),
            Order.order_number.label("order_number"),
            Order.status.label("status"),
            Order.currency_code.label("currency_code"),
            Order.created_at.label("created_at"),
            Order.updated_at.label("updated_at"),
            OrderItem.partner_id.label("partner_id"),
            func.count(OrderItem.id).label("items_count"),
            func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity"),
            func.coalesce(func.sum(OrderItem.total_price), 0).label("merchant_total"),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.id == order_id,
            Order.deleted_at.is_(None),
            OrderItem.partner_id == partner_id,
        )
        .group_by(
            Order.id,
            Order.order_number,
            Order.status,
            Order.currency_code,
            Order.created_at,
            Order.updated_at,
            OrderItem.partner_id,
        )
        .limit(1)
    )
    row = (await session.execute(statement)).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    items = await _items_by_order(session, partner_id=partner_id, order_ids=[order_id])
    order_payload = _order_projection(dict(row), items.get(order_id, []))
    return {
        "order": order_payload,
        "items": order_payload["items"],
        "history": [
            {
                "status": order_payload["status"],
                "created_at": order_payload.get("updated_at") or order_payload.get("created_at"),
                "scope": "merchant_current_status",
            }
        ],
        "merchant_financial_summary": order_payload["financial"],
    }


def merchant_payload_forbidden_keys(payload: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in MERCHANT_FORBIDDEN_FIELDS:
                found.add(key)
            found.update(merchant_payload_forbidden_keys(value))
    elif isinstance(payload, list):
        for item in payload:
            found.update(merchant_payload_forbidden_keys(item))
    return found
