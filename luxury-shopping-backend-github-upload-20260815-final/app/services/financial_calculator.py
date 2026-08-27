from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MODEL_BY_TABLE
from ..models.domain import Order, Product, ProductVariant
from ..repositories.resources import serialize_record


MONEY_SCALE = Decimal("0.01")


def money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value if value is not None else "0"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_money_amount")
    if amount < 0:
        raise HTTPException(status_code=400, detail="negative_money_amount")
    return amount.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def money_or_zero(value: Any) -> Decimal:
    try:
        return money(value)
    except HTTPException:
        return Decimal("0.00")


LOCAL_PAYMENT_SUCCESS_STATUSES = ("confirmed", "approved", "paid", "completed")


def local_request_total(payload: dict[str, Any]) -> Decimal:
    """Return the customer-facing total for a local-shopping request."""

    for field in ("final_price", "estimated_price", "amount", "total"):
        value = money_or_zero(payload.get(field))
        if value > 0:
            return value
    return Decimal("0.00")


def derive_local_payment_status(total: Any, paid: Any, existing_status: Any = None) -> str:
    """Map the confirmed local-payment ledger to the status shown to customers."""

    normalized_total = money_or_zero(total)
    normalized_paid = money_or_zero(paid)
    legacy_status = str(existing_status or "").strip().lower()
    if normalized_total > 0 and normalized_paid >= normalized_total:
        return "paid"
    if normalized_paid > 0:
        return "partial"
    if legacy_status in {"partial_refund", "refunded"}:
        return legacy_status
    return "unpaid"


async def serialize_local_shopping_requests(
    session: AsyncSession,
    requests: list[Any],
) -> list[dict[str, Any]]:
    """Serialize local requests with payment totals derived from confirmed ledger rows."""

    payloads = [serialize_record(request) for request in requests]
    if not requests:
        return payloads

    request_ids = [str(request.id) for request in requests]
    payment_model = MODEL_BY_TABLE["order_payments"]
    result = await session.execute(
        select(
            payment_model.extra_data["local_request_id"].astext,
            func.coalesce(func.sum(payment_model.amount), 0),
        )
        .where(
            payment_model.deleted_at.is_(None),
            payment_model.extra_data["local_request_id"].astext.in_(request_ids),
            func.lower(payment_model.status).in_(LOCAL_PAYMENT_SUCCESS_STATUSES),
        )
        .group_by(payment_model.extra_data["local_request_id"].astext)
    )
    paid_by_request = {
        str(request_id): money_or_zero(amount)
        for request_id, amount in result.all()
        if request_id
    }

    for request, payload in zip(requests, payloads):
        total = local_request_total(payload)
        ledger_paid = paid_by_request.get(str(request.id), Decimal("0.00"))
        paid = max(ledger_paid, money_or_zero(payload.get("paid_amount")))
        payload["paid_amount"] = format(paid, "f")
        payload["remaining_balance"] = format(max(total - paid, Decimal("0.00")), "f")
        payload["payment_status"] = derive_local_payment_status(
            total,
            paid,
            payload.get("payment_status"),
        )
    return payloads


def request_hash(body: Any) -> str:
    payload = dict(body) if isinstance(body, dict) else {"body": body}
    payload.pop("idempotencyKey", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def advisory_xact_lock(session: AsyncSession, scope: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:scope)::bigint)"),
        {"scope": scope},
    )


@dataclass(frozen=True)
class CheckoutFinancials:
    subtotal: Decimal
    product_discount: Decimal
    coupon_discount: Decimal
    loyalty_discount: Decimal
    shipping_total: Decimal
    total: Decimal
    coupon_id: str | None
    shipping_source: str
    breakdown: dict[str, Any]

    @property
    def discount_total(self) -> Decimal:
        return money(self.product_discount + self.coupon_discount + self.loyalty_discount)


def unit_price(product: Product, variant: ProductVariant | None = None) -> Decimal:
    if variant is not None and variant.price is not None:
        return money(variant.price)
    return money(product.price)


def line_total(unit: Decimal, quantity: int) -> Decimal:
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="invalid_quantity")
    return money(unit * Decimal(quantity))


async def _coupon_discount(
    session: AsyncSession,
    *,
    code: str | None,
    subtotal: Decimal,
    user_id: uuid.UUID,
) -> tuple[Decimal, str | None, dict[str, Any]]:
    if not code:
        return Decimal("0.00"), None, {"source": "none"}
    coupon_model = MODEL_BY_TABLE["coupons"]
    usage_model = MODEL_BY_TABLE["coupon_usage"]
    result = await session.execute(
        select(coupon_model)
        .where(
            func.upper(coupon_model.code) == code.upper().strip(),
            coupon_model.is_active.is_(True),
            coupon_model.deleted_at.is_(None),
        )
        .with_for_update()
        .limit(1)
    )
    coupon = result.scalar_one_or_none()
    if coupon is None:
        raise HTTPException(status_code=404, detail="coupon_invalid")
    if coupon.expires_at and coupon.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=404, detail="coupon_expired")
    extra = dict(coupon.extra_data or {})
    usage_limit = int(extra.get("usage_limit") or extra.get("max_uses") or 0)
    if usage_limit > 0:
        used = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(usage_model)
                    .where(
                        usage_model.deleted_at.is_(None),
                        usage_model.extra_data["coupon_id"].astext == str(coupon.id),
                    )
                )
            ).scalar_one()
        )
        if used >= usage_limit:
            raise HTTPException(status_code=409, detail="coupon_usage_limit")
    per_user_limit = int(extra.get("per_user_limit") or 0)
    if per_user_limit > 0:
        user_used = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(usage_model)
                    .where(
                        usage_model.deleted_at.is_(None),
                        usage_model.user_id == user_id,
                        usage_model.extra_data["coupon_id"].astext == str(coupon.id),
                    )
                )
            ).scalar_one()
        )
        if user_used >= per_user_limit:
            raise HTTPException(status_code=409, detail="coupon_user_usage_limit")
    discount_type = str(extra.get("discount_type") or extra.get("type") or "fixed").lower().strip()
    raw_value = extra.get("discount_value") if extra.get("discount_value") is not None else coupon.amount
    if discount_type in {"percentage", "percent"}:
        percentage = min(money(raw_value or 0), Decimal("100.00"))
        discount = money(subtotal * percentage / Decimal("100"))
    elif discount_type == "free_shipping":
        discount = Decimal("0.00")
    else:
        discount = min(money(raw_value or 0), subtotal)
    return discount, str(coupon.id), {
        "source": "coupon",
        "code": coupon.code,
        "coupon_id": str(coupon.id),
        "discount_type": discount_type,
        "free_shipping": discount_type == "free_shipping",
    }


async def _loyalty_discount(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    requested_points: Any,
    eligible_amount: Decimal,
) -> tuple[Decimal, dict[str, Any]]:
    points = money_or_zero(requested_points)
    if points <= 0:
        return Decimal("0.00"), {"source": "none"}
    loyalty_model = MODEL_BY_TABLE["user_loyalty"]
    result = await session.execute(
        select(loyalty_model).where(loyalty_model.user_id == user_id).with_for_update().limit(1)
    )
    loyalty = result.scalar_one_or_none()
    balance = money_or_zero(loyalty.balance if loyalty is not None else 0)
    if balance < points:
        raise HTTPException(status_code=409, detail="insufficient_loyalty_points")
    discount = min(points, eligible_amount)
    return discount, {"source": "user_loyalty", "points": str(points), "balance_before": str(balance)}


async def _shipping_total(session: AsyncSession, body: dict[str, Any]) -> tuple[Decimal, str, dict[str, Any]]:
    zones_model = MODEL_BY_TABLE["shipping_zones"]
    zone_id = body.get("shippingZoneId") or body.get("shipping_zone_id")
    if not zone_id and isinstance(body.get("shippingAddress"), dict):
        address = body["shippingAddress"]
        zone_id = address.get("shippingZoneId") or address.get("shipping_zone_id")
    if not zone_id and isinstance(body.get("shippingSelection"), dict):
        selection = body["shippingSelection"]
        zone_id = selection.get("shippingZoneId") or selection.get("shipping_zone_id")
    if zone_id:
        try:
            parsed_zone_id = uuid.UUID(str(zone_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_uuid:shippingZoneId")
        row = await session.get(zones_model, parsed_zone_id)
        if row is None or not row.is_active or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="shipping_zone_not_found")
        return money(row.fee or 0), "shipping_zone", {"shipping_zone_id": str(row.id)}
    raise HTTPException(status_code=422, detail="shipping_zone_required")


async def calculate_checkout_financials(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    subtotal: Decimal,
    body: dict[str, Any],
    product_discount: Decimal = Decimal("0.00"),
) -> CheckoutFinancials:
    subtotal = money(subtotal)
    product_discount = min(money(product_discount), subtotal)
    merchandise_total = money(subtotal - product_discount)
    coupon_code = body.get("couponCode") or body.get("coupon_code")
    coupon_discount, coupon_id, coupon_meta = await _coupon_discount(
        session, code=str(coupon_code).strip() if coupon_code else None, subtotal=merchandise_total, user_id=user_id
    )
    after_coupon = max(merchandise_total - coupon_discount, Decimal("0.00"))
    loyalty_discount, loyalty_meta = await _loyalty_discount(
        session,
        user_id=user_id,
        requested_points=body.get("loyaltyPointsToRedeem") or body.get("loyaltyPoints"),
        eligible_amount=after_coupon,
    )
    shipping_total, shipping_source, shipping_meta = await _shipping_total(session, body)
    if coupon_meta.get("free_shipping"):
        shipping_total = Decimal("0.00")
        shipping_meta = {**shipping_meta, "free_shipping_coupon": True}
    total = money(max(after_coupon - loyalty_discount, Decimal("0.00")) + shipping_total)
    breakdown = {
        "policy": "backend_decimal_half_up_2dp",
        "subtotal": str(subtotal),
        "product_discount": str(product_discount),
        "merchandise_total": str(merchandise_total),
        "coupon_discount": str(coupon_discount),
        "loyalty_discount": str(loyalty_discount),
        "shipping_total": str(shipping_total),
        "total": str(total),
        "coupon": coupon_meta,
        "loyalty": loyalty_meta,
        "shipping": shipping_meta,
        "ignored_client_fields": [
            key
            for key in ("subtotal", "discount", "couponDiscount", "loyaltyDiscount", "total", "grand_total", "shippingCost")
            if key in body
        ],
    }
    return CheckoutFinancials(
        subtotal=subtotal,
        product_discount=product_discount,
        coupon_discount=coupon_discount,
        loyalty_discount=loyalty_discount,
        shipping_total=shipping_total,
        total=total,
        coupon_id=coupon_id,
        shipping_source=shipping_source,
        breakdown=breakdown,
    )


async def receipt_amount_for_order(session: AsyncSession, order: Order, raw_amount: Any) -> Decimal:
    amount = money(raw_amount if raw_amount is not None else order.total)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="payment_amount_required")
    receipts_model = MODEL_BY_TABLE["payment_receipts"]
    reserved = (
        await session.execute(
            select(func.coalesce(func.sum(receipts_model.amount), 0))
            .where(
                receipts_model.order_id == order.id,
                receipts_model.deleted_at.is_(None),
                receipts_model.status.in_(["pending", "pending_review", "uploaded", "reviewing", "approved"]),
            )
        )
    ).scalar_one()
    outstanding = money(order.total) - money_or_zero(reserved)
    if amount > outstanding:
        raise HTTPException(status_code=409, detail="payment_exceeds_order_balance")
    return amount


async def approved_payment_total(session: AsyncSession, order_id: uuid.UUID) -> Decimal:
    receipts_model = MODEL_BY_TABLE["payment_receipts"]
    amount = (
        await session.execute(
            select(func.coalesce(func.sum(receipts_model.amount), 0))
            .where(
                receipts_model.order_id == order_id,
                receipts_model.deleted_at.is_(None),
                receipts_model.status == "approved",
            )
        )
    ).scalar_one()
    return money_or_zero(amount)


async def refunded_total(session: AsyncSession, order_id: uuid.UUID) -> Decimal:
    refunds_model = MODEL_BY_TABLE["refunds"]
    amount = (
        await session.execute(
            select(func.coalesce(func.sum(refunds_model.amount), 0))
            .where(
                refunds_model.order_id == order_id,
                refunds_model.deleted_at.is_(None),
                refunds_model.status.in_(["completed", "succeeded", "provider_succeeded", "manual_completed"]),
            )
        )
    ).scalar_one()
    return money_or_zero(amount)


async def sync_order_payment_status(session: AsyncSession, order: Order) -> None:
    paid = await approved_payment_total(session, order.id)
    refunded = await refunded_total(session, order.id)
    total = money(order.total)
    if refunded >= paid and paid > 0:
        order.payment_status = "refunded"
    elif refunded > 0:
        order.payment_status = "partially_refunded"
    elif paid >= total and total > 0:
        order.payment_status = "paid"
    elif paid > 0:
        order.payment_status = "partial"
    else:
        order.payment_status = "pending"


def financial_response_row(row: Any) -> dict[str, Any]:
    payload = serialize_record(row)
    for key in ("amount", "total", "subtotal", "discount_total", "shipping_total"):
        if key in payload and payload[key] is not None:
            payload[key] = str(money(payload[key]))
    return payload


async def find_idempotent_refund(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    endpoint: str,
    key: str,
    request_digest: str,
) -> Any | None:
    refunds_model = MODEL_BY_TABLE["refunds"]
    result = await session.execute(
        select(refunds_model).where(
            refunds_model.extra_data["idempotency_key"].astext == key,
            refunds_model.extra_data["idempotency_actor_id"].astext == str(actor_id),
            refunds_model.extra_data["idempotency_endpoint"].astext == endpoint,
        ).limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if (row.extra_data or {}).get("idempotency_request_hash") != request_digest:
        raise HTTPException(status_code=409, detail="idempotency_key_conflict")
    return row
