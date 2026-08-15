from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import Category, Product, ProductVariant
from .catalog_policy import is_public_approval_status, is_public_product
from .financial_calculator import money, unit_price


_PHONE_RE = re.compile(r"^\+?[0-9\s\-]{7,20}$")
@dataclass(frozen=True)
class EligibleLine:
    product: Product
    variant: ProductVariant | None
    quantity: int
    unit_price: Decimal
    available_stock: int
    max_quantity: int


def parse_strict_quantity(value: Any, *, field: str = "quantity") -> int:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail="invalid_quantity")
    if isinstance(value, int):
        quantity = value
    elif isinstance(value, str) and value.strip().isdigit():
        quantity = int(value.strip())
    else:
        raise HTTPException(status_code=400, detail="invalid_quantity")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="invalid_quantity")
    max_quantity = get_settings().cart_max_quantity_per_item
    if quantity > max_quantity:
        raise HTTPException(status_code=409, detail="quantity_limit_exceeded")
    return quantity


def validate_payment_method(value: Any) -> str:
    method = str(value or "").strip()
    if not method:
        raise HTTPException(status_code=422, detail="invalid_payment_method")
    allowed = get_settings().payment_method_allowlist
    if method not in allowed and method.upper() not in allowed:
        raise HTTPException(status_code=422, detail="invalid_payment_method")
    return method


def validate_shipping_address(value: Any) -> dict[str, Any]:
    address = value if isinstance(value, dict) else {}
    recipient = str(
        address.get("recipientName")
        or address.get("recipient_name")
        or address.get("fullName")
        or address.get("full_name")
        or address.get("name")
        or ""
    ).strip()
    phone = str(address.get("phone") or address.get("recipientPhone") or address.get("recipient_phone") or "").strip()
    city = str(address.get("city") or "").strip()
    street = str(address.get("address") or address.get("street") or "").strip()
    if not recipient:
        raise HTTPException(status_code=422, detail="shipping_recipient_required")
    if not phone or not _PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail="shipping_phone_invalid")
    if not city:
        raise HTTPException(status_code=422, detail="shipping_city_required")
    if not street:
        raise HTTPException(status_code=422, detail="shipping_address_required")
    normalized = dict(address)
    normalized["recipientName"] = recipient
    normalized["phone"] = phone
    normalized["city"] = city
    normalized["address"] = street
    return normalized


def validate_customer_checkout_address(value: Any) -> dict[str, Any]:
    """Validate the complete address required for a customer checkout."""
    normalized = validate_shipping_address(value)
    address = value if isinstance(value, dict) else {}
    governorate = str(
        address.get("governorate")
        or address.get("state")
        or address.get("province")
        or ""
    ).strip()
    if not governorate:
        raise HTTPException(status_code=422, detail="shipping_governorate_required")
    normalized["governorate"] = governorate
    return normalized


def _product_error(product: Product | None) -> HTTPException | None:
    if product is None or product.deleted_at is not None:
        return HTTPException(status_code=404, detail="product_not_available")
    if product.is_active is False:
        return HTTPException(status_code=409, detail="product_inactive")
    if not is_public_approval_status(product.approval_status):
        return HTTPException(status_code=409, detail="product_not_approved")
    if money(product.price) <= 0:
        return HTTPException(status_code=409, detail="product_not_available")
    return None


async def assert_partner_storefront_available(session: AsyncSession, product: Product) -> None:
    if not product.partner_id:
        return
    storefront_model = MODEL_BY_TABLE.get("partner_storefronts")
    if storefront_model is None:
        return
    result = await session.execute(
        select(storefront_model)
        .where(
            or_(storefront_model.user_id == product.partner_id, storefront_model.partner_id == product.partner_id),
            storefront_model.deleted_at.is_(None),
        )
        .limit(1)
    )
    storefront = result.scalar_one_or_none()
    if storefront is not None and getattr(storefront, "is_active", True) is not True:
        raise HTTPException(status_code=409, detail="merchant_not_active")


async def assert_category_available(session: AsyncSession, product: Product) -> None:
    if not product.category_id:
        return
    category = await session.get(Category, product.category_id)
    if category is None or category.deleted_at is not None or category.is_active is not True:
        raise HTTPException(status_code=409, detail="product_not_available")


async def validate_product_for_sale(
    session: AsyncSession,
    product: Product | None,
) -> Product:
    error = _product_error(product)
    if error is not None:
        raise error
    assert product is not None
    if not is_public_product(product):
        raise HTTPException(status_code=409, detail="product_not_available")
    await assert_category_available(session, product)
    await assert_partner_storefront_available(session, product)
    return product


async def validate_variant_for_sale(
    *,
    product: Product,
    variant: ProductVariant | None,
    requested_variant_id: uuid.UUID | None,
) -> ProductVariant | None:
    if requested_variant_id is None:
        return None
    if variant is None or variant.deleted_at is not None:
        raise HTTPException(status_code=404, detail="variant_not_available")
    if variant.product_id != product.id:
        raise HTTPException(status_code=409, detail="variant_product_mismatch")
    if variant.is_active is not True:
        raise HTTPException(status_code=409, detail="variant_not_active")
    if variant.price is not None and money(variant.price) <= 0:
        raise HTTPException(status_code=409, detail="variant_not_available")
    return variant


async def eligible_line(
    session: AsyncSession,
    *,
    product: Product | None,
    variant: ProductVariant | None = None,
    variant_id: uuid.UUID | None = None,
    quantity: int,
) -> EligibleLine:
    product = await validate_product_for_sale(session, product)
    variant = await validate_variant_for_sale(product=product, variant=variant, requested_variant_id=variant_id)
    unit = unit_price(product, variant)
    available = variant.stock_quantity if variant is not None else product.stock_quantity
    hard_limit = get_settings().cart_max_quantity_per_item
    max_quantity = min(hard_limit, int(available)) if product.track_inventory else hard_limit
    if quantity > max_quantity:
        raise HTTPException(status_code=409, detail="insufficient_stock" if product.track_inventory else "quantity_limit_exceeded")
    return EligibleLine(
        product=product,
        variant=variant,
        quantity=quantity,
        unit_price=unit,
        available_stock=int(available),
        max_quantity=max_quantity,
    )


def require_shipping_zone_id(body: dict[str, Any]) -> uuid.UUID:
    raw = body.get("shippingZoneId") or body.get("shipping_zone_id")
    if not raw and isinstance(body.get("shippingAddress"), dict):
        address = body["shippingAddress"]
        raw = address.get("shippingZoneId") or address.get("shipping_zone_id")
    if not raw and isinstance(body.get("shippingSelection"), dict):
        selection = body["shippingSelection"]
        raw = selection.get("shippingZoneId") or selection.get("shipping_zone_id")
    if not raw:
        raise HTTPException(status_code=422, detail="shipping_zone_required")
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_uuid:shippingZoneId")
