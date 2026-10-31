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
from ..models.domain import Category, Order, OrderItem, Product, ProductVariant
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
    if storefront is None or getattr(storefront, "is_active", True) is not True:
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
    allow_out_of_stock: bool = False,
) -> EligibleLine:
    product = await validate_product_for_sale(session, product)
    variant = await validate_variant_for_sale(product=product, variant=variant, requested_variant_id=variant_id)
    unit = unit_price(product, variant)
    available = variant.stock_quantity if variant is not None else product.stock_quantity
    hard_limit = get_settings().cart_max_quantity_per_item
    max_quantity = (
        hard_limit
        if allow_out_of_stock
        else min(hard_limit, int(available))
        if product.track_inventory
        else hard_limit
    )
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


def _stock_reference(data: dict[str, Any], *keys: str) -> str:
    return next((str(data[key]).strip() for key in keys if data.get(key)), "")


def _stock_amount(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _record_warehouse_movement(session: AsyncSession, *, product: Product,
                               item: OrderItem, location: Any, quantity: int,
                               movement_type: str) -> None:
    model = MODEL_BY_TABLE["inventory_movements"]
    session.add(model(
        product_id=product.id, variant_id=item.variant_id, quantity=quantity,
        type=movement_type, status="completed",
        notes="خصم الطلب" if movement_type == "out" else "إعادة مخزون الطلب الملغي",
        extra_data={"order_id": str(item.order_id), "order_item_id": str(item.id),
                    "warehouse_id": _stock_reference(location.extra_data or {}, "warehouse_id", "warehouseId"),
                    "inventory_location_id": str(location.id), "source": "order_inventory"},
    ))


async def deduct_order_inventory(session: AsyncSession, *, product: Product,
                                 variant: ProductVariant | None, item: OrderItem) -> None:
    """Deduct only this product's stock and retain its exact source for cancellation."""
    if not product.track_inventory:
        return
    model = MODEL_BY_TABLE["inventory_locations"]
    locations = list((await session.execute(select(model).where(
        model.deleted_at.is_(None),
        or_(model.extra_data["product_id"].astext == str(product.id),
            model.extra_data["productId"].astext == str(product.id)),
    ).order_by(model.created_at, model.id).with_for_update())).scalars())
    variant_id = str(variant.id) if variant else ""
    explicit = [row for row in locations if _stock_reference(row.extra_data or {}, "variant_id", "variantId") == variant_id]
    if variant and not explicit:
        explicit = [row for row in locations if not _stock_reference(row.extra_data or {}, "variant_id", "variantId")]
    locations = explicit
    warehouse_model = MODEL_BY_TABLE["warehouses"]
    warehouse_ids = {_stock_reference(row.extra_data or {}, "warehouse_id", "warehouseId") for row in locations}
    warehouse_ids.discard("")
    warehouses = {str(row.id): row for row in (await session.execute(select(warehouse_model).where(
        warehouse_model.id.in_([uuid.UUID(value) for value in warehouse_ids]),
    ))).scalars()} if warehouse_ids else {}
    eligible = []
    for row in locations:
        data = row.extra_data or {}
        warehouse_id = _stock_reference(data, "warehouse_id", "warehouseId")
        warehouse = warehouses.get(warehouse_id)
        if row.is_active is False:
            continue
        if warehouse_id and (warehouse is None or warehouse.deleted_at is not None or warehouse.is_active is False):
            continue
        owner = _stock_reference(warehouse.extra_data or {}, "partner_id", "partnerId", "owner_id", "user_id") if warehouse else ""
        if owner and owner != str(product.partner_id or ""):
            continue
        eligible.append(row)
    assigned = sum(_stock_amount((row.extra_data or {}).get("quantity")) for row in locations)
    stock = variant.stock_quantity if variant else product.stock_quantity
    unassigned = max(_stock_amount(stock) - assigned, 0)
    available = unassigned + sum(_stock_amount((row.extra_data or {}).get("quantity")) for row in eligible)
    if available < item.quantity:
        raise HTTPException(status_code=409, detail="insufficient_stock")
    remaining = item.quantity
    allocation = []
    for row in eligible:
        data = dict(row.extra_data or {})
        taken = min(remaining, _stock_amount(data.get("quantity")))
        if not taken:
            continue
        data["quantity"] = _stock_amount(data.get("quantity")) - taken
        row.extra_data = data
        allocation.append({"location_id": str(row.id),
                           "warehouse_id": _stock_reference(data, "warehouse_id", "warehouseId"),
                           "quantity": taken})
        _record_warehouse_movement(session, product=product, item=item,
                                   location=row, quantity=taken, movement_type="out")
        remaining -= taken
        if not remaining:
            break
    product_delta = item.quantity if variant is None or locations else 0
    product_base = _stock_amount(product.stock_quantity)
    if variant is not None:
        if product_delta:
            product_base = max(product_base, _stock_amount(variant.stock_quantity))
        variant.stock_quantity -= item.quantity
    if product_delta:
        product.stock_quantity = product_base - product_delta
    item.extra_data = {**(item.extra_data or {}), "stock_allocation": {
        "product_id": str(product.id), "variant_id": variant_id,
        "locations": allocation, "unassigned_quantity": remaining,
        "product_quantity_deducted": product_delta, "restored": False,
    }}


async def restore_order_inventory(session: AsyncSession, order: Order) -> None:
    items = list((await session.execute(select(OrderItem).where(
        OrderItem.order_id == order.id,
    ).order_by(OrderItem.product_id, OrderItem.variant_id, OrderItem.id).with_for_update())).scalars())
    for item in items:
        data = dict(item.extra_data or {})
        allocation = data.get("stock_allocation")
        if data.get("stock_restored") is True or isinstance(allocation, dict) and allocation.get("restored") is True:
            continue
        product = await session.get(Product, item.product_id, with_for_update=True)
        if product is None:
            continue
        variant = await session.get(ProductVariant, item.variant_id, with_for_update=True) if item.variant_id else None
        if isinstance(allocation, dict):
            if allocation.get("product_id") != str(item.product_id) or allocation.get("variant_id", "") != str(item.variant_id or ""):
                raise HTTPException(status_code=409, detail="stock_allocation_invalid")
            model = MODEL_BY_TABLE["inventory_locations"]
            for source in sorted(allocation.get("locations", []), key=lambda value: value["location_id"]):
                row = await session.get(model, uuid.UUID(source["location_id"]), with_for_update=True)
                if row is None or row.deleted_at is not None:
                    raise HTTPException(status_code=409, detail="stock_location_unavailable")
                payload = dict(row.extra_data or {})
                if _stock_reference(payload, "product_id", "productId") != str(item.product_id) or _stock_reference(payload, "warehouse_id", "warehouseId") != source["warehouse_id"]:
                    raise HTTPException(status_code=409, detail="stock_allocation_invalid")
                quantity = _stock_amount(source["quantity"])
                payload["quantity"] = _stock_amount(payload.get("quantity")) + quantity
                row.extra_data = payload
                _record_warehouse_movement(session, product=product, item=item,
                                           location=row, quantity=quantity, movement_type="in")
            product.stock_quantity += _stock_amount(allocation.get("product_quantity_deducted"))
            if variant is not None:
                variant.stock_quantity += item.quantity
            data["stock_allocation"] = {**allocation, "restored": True}
        elif product.track_inventory:
            if variant is not None:
                variant.stock_quantity += item.quantity
            elif not item.variant_id:
                product.stock_quantity += item.quantity
        data["stock_restored"] = True
        item.extra_data = data
