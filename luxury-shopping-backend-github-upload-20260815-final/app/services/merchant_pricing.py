"""Merchant net prices use the same contract and rounding as the settlement portal."""
import uuid
from decimal import Decimal, ROUND_CEILING
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Numeric, case, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MODEL_BY_TABLE
from ..models.domain import Product, ProductVariant
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
