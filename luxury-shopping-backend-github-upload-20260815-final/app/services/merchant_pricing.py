"""Merchant net prices use the same contract and rounding as the settlement portal."""
import uuid
from decimal import Decimal, ROUND_CEILING
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Numeric, case, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MODEL_BY_TABLE
from ..models.domain import Order, OrderItem, Product, ProductVariant
from .financial_calculator import money


async def latest_partner_contract(
    session: AsyncSession,
    partner_id: uuid.UUID,
    *,
    for_update: bool = False,
):
    model = MODEL_BY_TABLE["partner_contracts"]
    statement = (
        select(model)
        .where(model.partner_id == partner_id, model.deleted_at.is_(None))
        .order_by(model.updated_at.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalar_one_or_none()


def partner_rate(value: Any, default: int = 85) -> int | float:
    if value is None or str(value).strip() == "":
        return default
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="invalid_commission_rate")
    if not 0 <= rate <= 100:
        raise HTTPException(status_code=422, detail="invalid_commission_rate")
    return int(rate) if rate.is_integer() else rate


def contract_share(contract) -> Decimal:
    data = getattr(contract, "extra_data", None)
    data = data if isinstance(data, dict) else {}
    return Decimal(str(partner_rate(data.get("commissionRate", data.get("rate")))))


async def sync_order_partner_settlements(
    session: AsyncSession, order: Order, *, reopened: bool = False
) -> None:
    """Record one contract-based payable per merchant on a paid, delivered order.

    Serialize on the order row before locking existing settlements. Lock any
    linked settlements as well so an accounting payout cannot race a reversal.
    """
    delivered = order.status in {"delivered", "completed"}
    if not delivered and not reopened:
        return

    await session.execute(select(Order.id).where(Order.id == order.id).with_for_update())
    eligible = delivered and order.deleted_at is None and order.payment_status == "paid"
    model = MODEL_BY_TABLE["partner_settlements"]
    existing = (await session.execute(
        select(model).where(
            model.deleted_at.is_(None),
            model.extra_data["order_id"].astext == str(order.id),
        ).order_by(model.id).with_for_update()
    )).scalars().all()
    if not eligible:
        for row in existing:
            if row.status in {"pending", "unpaid"}:
                row.status = "cancelled"
        return

    by_partner = {row.partner_id: row for row in existing}
    totals = (await session.execute(
        select(OrderItem.partner_id, func.sum(OrderItem.total_price))
        .where(OrderItem.order_id == order.id, OrderItem.partner_id.is_not(None))
        .group_by(OrderItem.partner_id)
    )).all()
    for partner_id, total in totals:
        previous = by_partner.get(partner_id)
        if previous is not None:
            if previous.status == "cancelled":
                previous.status = "pending"
            continue

        contract = await latest_partner_contract(session, partner_id)
        share = contract_share(contract)
        gross = money(total)
        payable = money(gross * share / Decimal(100))
        if payable == 0:
            continue
        session.add(model(
            partner_id=partner_id,
            status="pending",
            amount=payable,
            extra_data={
                "order_id": str(order.id),
                "order_number": order.order_number,
                "currency_code": order.currency_code or "YER",
                "order_amount": str(gross),
                "commission_rate": str(Decimal(100) - share),
                "commission_amount": str(gross - payable),
                "partner_share_rate": str(share),
                "partner_amount": str(payable),
            },
        ))


def retail_price(net, share: Decimal, currency: str) -> Decimal:
    if share <= 0:
        raise HTTPException(422, "لا يمكن حساب سعر البيع لأن حصة التاجر صفر")
    quantum = Decimal("100") if currency.upper() == "YER" else Decimal("0.01")
    return money((net * 100 / (share * quantum)).to_integral_value(rounding=ROUND_CEILING) * quantum)


async def reprice_merchant_catalog(session, partner_id, share: Decimal):
    # A zero share has no finite retail equivalent. Existing retail prices and
    # historical order lines stay intact. A later positive share can reprice them.
    if share <= 0:
        return
    for model in (Product, ProductVariant):
        if model is Product:
            owner = Product.partner_id == partner_id
            currency = Product.currency_code
        else:
            owner = ProductVariant.product_id.in_(select(Product.id).where(Product.partner_id == partner_id))
            currency = select(Product.currency_code).where(Product.id == ProductVariant.product_id).scalar_subquery()
        quantum = case((func.upper(currency) == "YER", 100), else_=Decimal("0.01"))
        pricing = model.extra_data["merchant_pricing"]
        fields = {}
        for key in ("price", "original_price"):
            net = cast(pricing[key].astext, Numeric)
            fields[key] = case((net.is_not(None), func.ceil(net * 100 / (share * quantum)) * quantum), else_=getattr(model, key))
        await session.execute(update(model).where(owner, model.deleted_at.is_(None), model.extra_data.has_key("merchant_pricing")).values(**fields).execution_options(synchronize_session=False))
