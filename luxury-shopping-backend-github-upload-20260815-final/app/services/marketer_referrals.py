import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MODEL_BY_TABLE
from ..models.domain import Order, User
from .financial_calculator import money


PAYABLE_COMMISSION_STATUSES = ("pending", "earned", "approved", "unpaid")


async def active_marketer_for_code(session: AsyncSession, code: object):
    if not isinstance(code, str):
        return None
    normalized = code.strip().upper()
    if not 1 <= len(normalized) <= 100:
        return None

    marketer = MODEL_BY_TABLE["marketers"]
    public_code = MODEL_BY_TABLE["public_marketer_codes"]
    profile_code = func.coalesce(
        func.nullif(marketer.extra_data["referral_code"].astext, ""),
        marketer.extra_data["code"].astext,
    )
    alternate_code = (
        select(public_code.id)
        .where(
            public_code.user_id == marketer.user_id,
            public_code.deleted_at.is_(None),
            public_code.is_active.is_(True),
            public_code.status == "active",
            func.upper(func.trim(public_code.code)) == normalized,
        )
        .exists()
    )
    result = await session.execute(
        select(marketer.id, marketer.user_id, marketer.name)
        .join(User, User.id == marketer.user_id)
        .where(
            marketer.deleted_at.is_(None),
            marketer.status == "active",
            User.deleted_at.is_(None),
            User.is_active.is_(True),
            or_(func.upper(func.trim(profile_code)) == normalized, alternate_code),
        )
        .limit(2)
    )
    rows = result.all()
    return rows[0] if len(rows) == 1 else None


async def sync_order_marketer_commission(session: AsyncSession, order: Order) -> None:
    raw_marketer_id = (order.extra_data or {}).get("marketer_id")
    if not raw_marketer_id:
        return
    try:
        marketer_id = uuid.UUID(str(raw_marketer_id))
    except ValueError:
        return

    marketer = await session.get(MODEL_BY_TABLE["marketers"], marketer_id)
    if marketer is None or marketer.user_id is None or marketer.user_id == order.user_id:
        return

    # Use the same lock order as merchant settlement: the order, then the payable.
    await session.execute(select(Order.id).where(Order.id == order.id).with_for_update())
    model = MODEL_BY_TABLE["marketer_commissions"]
    commissions = (
        await session.scalars(
            select(model)
            .where(model.order_id == order.id, model.deleted_at.is_(None))
            .order_by(model.id)
            .with_for_update()
        )
    ).all()
    eligible = (
        order.deleted_at is None
        and order.status in {"delivered", "completed"}
        and order.payment_status == "paid"
    )
    if not eligible:
        for commission in commissions:
            if commission.user_id == marketer.user_id and commission.status in PAYABLE_COMMISSION_STATUSES:
                commission.status = "cancelled"
                commission.extra_data = {**(commission.extra_data or {}), "cancelled_by_order_state": True}
        return

    if commissions:
        for commission in commissions:
            extra = dict(commission.extra_data or {})
            if (
                commission.user_id == marketer.user_id
                and commission.status == "cancelled"
                and extra.pop("cancelled_by_order_state", False)
            ):
                commission.status = "pending"
                commission.extra_data = extra
        return

    # The tracked storefront checkout and the marketer payout ledger both use YER.
    if order.currency_code != "YER":
        return
    try:
        rate = Decimal(str((marketer.extra_data or {}).get("commission_rate", 0)))
    except InvalidOperation:
        return
    if not rate.is_finite() or not 0 < rate <= 100:
        return

    order_amount = money(order.total)
    amount = money(order_amount * rate / Decimal(100))
    if not amount:
        return

    commission = model(
        user_id=marketer.user_id,
        order_id=order.id,
        status="pending",
        amount=amount,
        extra_data={
            "order_amount": str(order_amount),
            "commission_rate": str(rate),
            "currency_code": order.currency_code,
            "order_number": order.order_number,
        },
    )
    session.add(commission)
    await session.flush([commission])


async def marketer_commission_totals(session: AsyncSession, user_ids: list[uuid.UUID]):
    if not user_ids:
        return {}
    model = MODEL_BY_TABLE["marketer_commissions"]
    payable = model.status.in_(PAYABLE_COMMISSION_STATUSES)
    recorded = model.status.in_((*PAYABLE_COMMISSION_STATUSES, "paid"))
    rows = await session.execute(
        select(
            model.user_id,
            func.count(func.distinct(model.order_id)).filter(recorded).label("purchase_count"),
            func.coalesce(func.sum(Order.total).filter(recorded), 0).label("total_sales"),
            func.coalesce(func.sum(model.amount).filter(recorded), 0).label("total_commission"),
            func.coalesce(func.sum(model.amount).filter(payable), 0).label("pending_commission"),
        )
        .outerjoin(Order, Order.id == model.order_id)
        .where(model.user_id.in_(user_ids), model.deleted_at.is_(None))
        .group_by(model.user_id)
    )
    return {row.user_id: row for row in rows}
