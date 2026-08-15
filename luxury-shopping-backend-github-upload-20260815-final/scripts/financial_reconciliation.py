from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import SessionFactory
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Order, OrderItem, Product, ProductVariant
from backend.app.services.financial_calculator import approved_payment_total, money, refunded_total


def _amount(value: Any) -> Decimal:
    return money(value or Decimal("0.00"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(_amount(value))
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


async def _sum_order_items(session: AsyncSession, order_id: uuid.UUID) -> Decimal:
    value = (
        await session.execute(
            select(func.coalesce(func.sum(OrderItem.total_price), 0)).where(OrderItem.order_id == order_id)
        )
    ).scalar_one()
    return _amount(value)


async def _count_rows(session: AsyncSession, table_name: str, **filters: Any) -> int:
    model = MODEL_BY_TABLE[table_name]
    statement = select(func.count()).select_from(model)
    for column_name, value in filters.items():
        column = model.__table__.c.get(column_name)
        if column is not None:
            statement = statement.where(column == value)
    return int((await session.execute(statement)).scalar_one())


async def _negative_amount_issues(session: AsyncSession) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for table_name in (
        "orders",
        "order_items",
        "order_payments",
        "payments",
        "payment_receipts",
        "refunds",
        "coupon_usage",
        "points_transactions",
        "partner_wallets",
        "partner_payments",
        "marketer_commissions",
        "marketer_payments",
        "inventory_movements",
    ):
        model = MODEL_BY_TABLE.get(table_name)
        if model is None:
            continue
        for column_name in ("total", "subtotal", "discount_total", "shipping_total", "amount", "balance", "quantity_after"):
            column = model.__table__.c.get(column_name)
            if column is None:
                continue
            count = int(
                (
                    await session.execute(
                        select(func.count()).select_from(model).where(column < 0)
                    )
                ).scalar_one()
            )
            if count:
                issues.append(
                    {
                        "type": "negative_value",
                        "table": table_name,
                        "column": column_name,
                        "count": count,
                    }
                )
    return issues


async def _orphan_issues(session: AsyncSession) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    order_ids = select(Order.id)
    for table_name in (
        "order_items",
        "order_payments",
        "payments",
        "payment_receipts",
        "refunds",
        "coupon_usage",
        "points_transactions",
    ):
        model = MODEL_BY_TABLE.get(table_name)
        if model is None or "order_id" not in model.__table__.c:
            continue
        count = int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.__table__.c.order_id.not_in(order_ids))
                )
            ).scalar_one()
        )
        if count:
            issues.append({"type": "orphan_order_reference", "table": table_name, "count": count})
    return issues


async def _load_orders(
    session: AsyncSession,
    *,
    order_ids: Iterable[uuid.UUID] | None = None,
    prefix: str | None = None,
    limit: int = 500,
) -> list[Order]:
    statement = select(Order).where(Order.deleted_at.is_(None))
    if order_ids:
        statement = statement.where(Order.id.in_(list(order_ids)))
    if prefix:
        pattern = f"%{prefix}%"
        statement = statement.where(
            Order.order_number.ilike(pattern)
            | Order.notes.ilike(pattern)
            | cast(Order.extra_data, String).ilike(pattern)
            | cast(Order.shipping_address, String).ilike(pattern)
        )
    result = await session.execute(statement.order_by(Order.created_at.desc()).limit(limit))
    return list(result.scalars())


async def reconcile_financial_integrity(
    *,
    order_ids: Iterable[uuid.UUID] | None = None,
    prefix: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    async with SessionFactory() as session:
        orders = await _load_orders(session, order_ids=order_ids, prefix=prefix, limit=limit)
        issues: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        totals = {
            "orders": Decimal("0.00"),
            "items": Decimal("0.00"),
            "discount": Decimal("0.00"),
            "shipping": Decimal("0.00"),
            "paid": Decimal("0.00"),
            "refunded": Decimal("0.00"),
        }
        for order in orders:
            item_total = await _sum_order_items(session, order.id)
            paid = await approved_payment_total(session, order.id)
            refunded = await refunded_total(session, order.id)
            subtotal = _amount(order.subtotal)
            discount = _amount(order.discount_total)
            shipping = _amount(order.shipping_total)
            total = _amount(order.total)
            expected_total = _amount(max(item_total - discount, Decimal("0.00")) + shipping)
            if subtotal != item_total:
                issues.append(
                    {
                        "type": "subtotal_mismatch",
                        "order_id": order.id,
                        "order_number": order.order_number,
                        "expected": item_total,
                        "actual": subtotal,
                    }
                )
            if total != expected_total:
                issues.append(
                    {
                        "type": "total_mismatch",
                        "order_id": order.id,
                        "order_number": order.order_number,
                        "expected": expected_total,
                        "actual": total,
                    }
                )
            if refunded > paid:
                issues.append(
                    {
                        "type": "refund_exceeds_paid",
                        "order_id": order.id,
                        "order_number": order.order_number,
                        "paid": paid,
                        "refunded": refunded,
                    }
                )
            item_count = await _count_rows(session, "order_items", order_id=order.id)
            if item_count == 0:
                issues.append({"type": "order_without_items", "order_id": order.id, "order_number": order.order_number})
            rows.append(
                {
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "subtotal": subtotal,
                    "items_total": item_total,
                    "discount": discount,
                    "shipping": shipping,
                    "total": total,
                    "expected_total": expected_total,
                    "paid": paid,
                    "refunded": refunded,
                    "payment_status": order.payment_status,
                    "item_count": item_count,
                }
            )
            totals["orders"] += total
            totals["items"] += item_total
            totals["discount"] += discount
            totals["shipping"] += shipping
            totals["paid"] += paid
            totals["refunded"] += refunded

        issues.extend(await _negative_amount_issues(session))
        issues.extend(await _orphan_issues(session))

        product_negative = int(
            (
                await session.execute(
                    select(func.count()).select_from(Product).where(Product.stock_quantity < 0)
                )
            ).scalar_one()
        )
        variant_negative = int(
            (
                await session.execute(
                    select(func.count()).select_from(ProductVariant).where(ProductVariant.stock_quantity < 0)
                )
            ).scalar_one()
        )
        if product_negative:
            issues.append({"type": "negative_product_stock", "count": product_negative})
        if variant_negative:
            issues.append({"type": "negative_variant_stock", "count": variant_negative})

        return _jsonable(
            {
                "status": "pass" if not issues else "fail",
                "scope": {
                    "order_ids": [str(item) for item in order_ids or []],
                    "prefix": prefix,
                    "limit": limit,
                },
                "summary": {
                    "orders_checked": len(orders),
                    "issue_count": len(issues),
                    "totals": totals,
                },
                "orders": rows,
                "issues": issues,
            }
        )


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Verify financial reconciliation against PostgreSQL.")
    parser.add_argument("--order-id", action="append", default=[])
    parser.add_argument("--prefix")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--output")
    args = parser.parse_args()
    parsed_ids = [uuid.UUID(value) for value in args.order_id]
    result = await reconcile_financial_integrity(order_ids=parsed_ids or None, prefix=args.prefix, limit=args.limit)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
