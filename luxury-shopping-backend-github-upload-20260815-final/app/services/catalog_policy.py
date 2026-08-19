from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import Brand, Category, Product, ProductVariant


PUBLIC_APPROVAL_STATUSES = ("approved", "accepted", "active", "published", "visible", "live")
PRIVATE_APPROVAL_STATUSES = (
    "archived",
    "blocked",
    "deleted",
    "disabled",
    "draft",
    "hidden",
    "inactive",
    "in_review",
    "awaiting_approval",
    "needs_review",
    "needs_content_review",
    "needs_image_review",
    "not_approved",
    "not-approved",
    "pending",
    "pending_approval",
    "rejected",
    "reviewing",
    "suspended",
    "under_review",
    "unapproved",
)
MERCHANT_BLOCKED_PRODUCT_FIELDS = frozenset(
    {
        "approval_status",
        "approvalStatus",
        "approval_notes",
        "approvalNotes",
        "approved_by",
        "approvedBy",
        "approved_at",
        "approvedAt",
        "rejection_reason",
        "rejectionReason",
        "moderation_notes",
        "moderationNotes",
        "is_featured",
        "isFeatured",
        "featured_at",
        "featuredAt",
        "featured_until",
        "featuredUntil",
        "is_promoted",
        "isPromoted",
        "promotional",
        "promotion_priority",
        "promotionPriority",
        "promotion_type",
        "promotionType",
        "homepage_priority",
        "homepagePriority",
        "sponsored",
        "featured_rank",
        "featuredRank",
    }
)
MERCHANT_SENSITIVE_PRODUCT_FIELDS = frozenset(
    {
        "name",
        "name_en",
        "description",
        "rich_description",
        "price",
        "original_price",
        "category_id",
        "brand_id",
        "sku",
        "image_url",
        "images",
    }
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,79}$")

_INTERNAL_VISIBLE_TEXT_PATTERNS = (
    re.compile(r"\bCODEX\b|CODEX_", re.IGNORECASE),
    re.compile(r"\bE2E\b|E2E_", re.IGNORECASE),
    re.compile(r"\bTEST\b|TEST_", re.IGNORECASE),
    re.compile(r"\bCART_ORDER_REMEDIATION\b", re.IGNORECASE),
    re.compile(r"\bMOCK\b|\bDUMMY\b|\bSAMPLE\b|\bFIXTURE\b|RUN_ID", re.IGNORECASE),
    re.compile(r"^Imported product\b", re.IGNORECASE),
    re.compile(r"^Unknown product\b|^Unknown item\b", re.IGNORECASE),
    re.compile(r"^Product\s+[0-9a-f_-]{5,}$", re.IGNORECASE),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE),
)


def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def safe_public_display_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text_value = value.strip()
    if not text_value:
        return False
    return not any(pattern.search(text_value) for pattern in _INTERNAL_VISIBLE_TEXT_PATTERNS)


def first_safe_display_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if safe_public_display_text(text):
                return text
    return None


def product_has_safe_public_text(product: Product) -> bool:
    return safe_public_display_text(product.name) or safe_public_display_text(product.name_en)


def public_approval_clause(model: type[Product] = Product) -> Any:
    approval = func.lower(func.trim(func.coalesce(model.approval_status, "")))
    return or_(
        approval == "",
        approval.in_(PUBLIC_APPROVAL_STATUSES),
        ~approval.in_(PRIVATE_APPROVAL_STATUSES),
    )


def is_public_approval_status(value: Any) -> bool:
    approval = str(value or "").strip().lower()
    return not approval or approval in PUBLIC_APPROVAL_STATUSES or approval not in PRIVATE_APPROVAL_STATUSES


def public_product_base_clauses(model: type[Product] = Product) -> list[Any]:
    return [
        model.deleted_at.is_(None),
        public_approval_clause(model),
        public_product_safe_text_clause(model),
    ]


def public_product_safe_text_clause(model: type[Product] = Product) -> Any:
    blocked = []
    for pattern in (
        "%CODEX%",
        "%E2E%",
        "%TEST%",
        "%CART_ORDER_REMEDIATION%",
        "%MOCK%",
        "%DUMMY%",
        "%SAMPLE%",
        "%FIXTURE%",
        "%RUN_ID%",
        "Imported product%",
        "%QA%",
        "%اختبار%",
        "%تحقق%",
        "منتج قديم مؤرشف%",
        "Unknown product%",
        "Unknown item%",
    ):
        blocked.append(func.coalesce(model.name, "").ilike(pattern))
        blocked.append(func.coalesce(model.name_en, "").ilike(pattern))
    visible_name = or_(
        and_(model.name.is_not(None), func.length(func.trim(model.name)) > 0),
        and_(model.name_en.is_not(None), func.length(func.trim(model.name_en)) > 0),
    )
    return and_(visible_name, ~or_(*blocked))


def public_product_clauses(model: type[Product] = Product) -> list[Any]:
    return public_product_base_clauses(model)


def new_product_clause(model: type[Product] = Product) -> Any:
    cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().new_product_days)
    return func.coalesce(model.approved_at, model.created_at) >= cutoff


def is_public_product(product: Product) -> bool:
    return (
        product.deleted_at is None
        and is_public_approval_status(product.approval_status)
    )


def _discount_percentage(price: Any, original_price: Any) -> int | None:
    try:
        current = Decimal(str(price or 0))
        original = Decimal(str(original_price or 0))
    except Exception:
        return None
    if original <= 0 or current < 0 or current >= original:
        return None
    return int(((original - current) / original * Decimal("100")).quantize(Decimal("1")))


def _stock_status(quantity: Any) -> str:
    try:
        value = int(quantity or 0)
    except Exception:
        return "out_of_stock"
    if value <= 0:
        return "out_of_stock"
    if value <= 5:
        return "low_stock"
    return "in_stock"


def public_category_summary(category: Category | None) -> dict[str, Any] | None:
    if category is None:
        return None
    return {
        "id": str(category.id),
        "name": category.name,
        "name_en": category.name_en,
        "slug": category.slug,
    }


def public_brand_summary(brand: Brand | None) -> dict[str, Any] | None:
    if brand is None:
        return None
    return {
        "id": str(brand.id),
        "name": brand.name,
        "name_en": brand.name_en,
        "slug": brand.slug,
        "logo_url": brand.logo_url,
    }


def public_variant_response(variant: ProductVariant) -> dict[str, Any]:
    images = [_public_upload_url(image) for image in (variant.images or [])]
    images = [image for image in images if image]
    image_url = _public_upload_url(variant.image_url) or (images[0] if images else None)
    return {
        "id": str(variant.id),
        "product_id": str(variant.product_id),
        "sku": variant.sku,
        "size": variant.size,
        "color": variant.color,
        "color_hex": variant.color_hex,
        "price": _json_value(variant.price),
        "original_price": _json_value(variant.original_price),
        "stock_quantity": int(variant.stock_quantity or 0),
        "stock_status": _stock_status(variant.stock_quantity),
        "image_url": image_url,
        "images": images,
        "is_active": variant.is_active is not False,
        "sort_order": int(variant.sort_order or 0),
    }


def public_storefront_response(row: Any, *, products_count: int | None = None) -> dict[str, Any]:
    name = getattr(row, "name", None) or getattr(row, "business_name", None) or "Merchant store"
    logo_value = (
        getattr(row, "logo_url", None)
        or getattr(row, "store_logo_url", None)
        or getattr(row, "image_url", None)
        or getattr(row, "avatar_url", None)
    )
    # Store images may be saved as a relative upload path or as the public CDN
    # URL returned by the upload endpoint. Normalize upload paths for clients,
    # while preserving a manually configured absolute URL for compatibility.
    logo_url = _public_upload_url(logo_value) or (
        str(logo_value).strip()
        if isinstance(logo_value, str) and logo_value.strip()
        else None
    )
    return {
        "id": str(getattr(row, "partner_id", None) or getattr(row, "user_id", None) or getattr(row, "id")),
        "display_name": name,
        "name": name,
        "name_en": getattr(row, "name_en", None),
        "slug": getattr(row, "slug", None),
        "logo_url": logo_url,
        "store_logo_url": logo_url,
        "cover_url": getattr(row, "cover_url", None),
        "public_description": getattr(row, "description", None),
        "description": getattr(row, "description", None),
        "public_city": getattr(row, "city", None),
        "city": getattr(row, "city", None),
        "category": getattr(row, "category", None),
        "rating": _json_value(getattr(row, "rating", None) or 0),
        "reviews_count": int(getattr(row, "reviews_count", None) or 0),
        "products_count": int(products_count or getattr(row, "product_count", None) or 0),
        "product_count": int(products_count or getattr(row, "product_count", None) or 0),
        "joined_at": _json_value(getattr(row, "created_at", None)),
    }


def public_main_storefront_response(*, products_count: int = 0) -> dict[str, Any]:
    return {
        "id": "main-store",
        "partner_id": None,
        "display_name": "المتجر الرئيسي",
        "name": "المتجر الرئيسي",
        "name_en": "Main Store",
        "slug": "main-store",
        "logo_url": None,
        "cover_url": None,
        "public_description": "منتجات رفاهية التسوق المعتمدة من الإدارة.",
        "description": "منتجات رفاهية التسوق المعتمدة من الإدارة.",
        "public_city": None,
        "city": None,
        "category": "main",
        "rating": 0,
        "reviews_count": 0,
        "products_count": int(products_count or 0),
        "product_count": int(products_count or 0),
        "joined_at": None,
        "is_main_store": True,
    }


def public_fallback_partner_storefront_response(
    partner_id: Any,
    *,
    name: str | None = None,
    products_count: int = 0,
) -> dict[str, Any]:
    partner_text = str(partner_id or "").strip()
    display_name = (name or "").strip() or f"متجر {partner_text[:8]}"
    return {
        "id": partner_text,
        "partner_id": partner_text,
        "display_name": display_name,
        "name": display_name,
        "name_en": f"Merchant {partner_text[:8]}",
        "slug": f"merchant-{partner_text[:8]}",
        "logo_url": None,
        "cover_url": None,
        "public_description": "متجر تاجر مرتبط بمنتجات معتمدة.",
        "description": "متجر تاجر مرتبط بمنتجات معتمدة.",
        "public_city": None,
        "city": None,
        "category": "merchant",
        "rating": 0,
        "reviews_count": 0,
        "products_count": int(products_count or 0),
        "product_count": int(products_count or 0),
        "joined_at": None,
        "is_generated_from_products": True,
    }


def public_product_response(
    product: Product,
    *,
    category: Category | None = None,
    brand: Brand | None = None,
    merchant: Any | None = None,
    variants: list[ProductVariant] | None = None,
) -> dict[str, Any]:
    images = [_public_upload_url(image) for image in (product.images or [])]
    images = [image for image in images if image]
    image_url = _public_upload_url(product.image_url) or (images[0] if images else None)
    primary_image = image_url or (images[0] if images else None)
    track_inventory = product.track_inventory is not False
    stock_quantity = int(product.stock_quantity or 0)
    is_publicly_orderable = (
        product.deleted_at is None
        and product.is_active is not False
        and is_public_approval_status(product.approval_status)
        and Decimal(str(product.price or 0)) > 0
        and (not track_inventory or stock_quantity > 0)
    )
    row = {
        "id": str(product.id),
        "short_code": product.short_code,
        "sku": product.sku,
        "slug": product.short_code or str(product.id),
        "name": product.name,
        "name_en": product.name_en,
        "description": product.description,
        "short_description": product.description,
        "public_description": product.description,
        "rich_description": product.rich_description,
        "price": _json_value(product.price),
        "original_price": _json_value(product.original_price),
        "currency_code": product.currency_code,
        "discount_percentage": _discount_percentage(product.price, product.original_price),
        "image_url": image_url,
        "imageUrl": image_url,
        "images": images,
        "primary_image": primary_image,
        "category": public_category_summary(category),
        "brand": public_brand_summary(brand),
        "supplier": public_storefront_response(merchant) if merchant is not None else None,
        "merchant": public_storefront_response(merchant) if merchant is not None else None,
        "partner_id": str(product.partner_id) if product.partner_id else None,
        "store_name": (
            getattr(merchant, "name", None)
            if merchant is not None
            else "المتجر الرئيسي"
        ),
        "store_name_en": (
            getattr(merchant, "name_en", None)
            if merchant is not None
            else "Main Store"
        ),
        "stock_quantity": stock_quantity,
        "stock_status": _stock_status(product.stock_quantity),
        "track_inventory": track_inventory,
        "is_active": product.is_active is not False,
        "approval_status": product.approval_status,
        "is_orderable": is_publicly_orderable,
        "is_available_for_checkout": is_publicly_orderable,
        "availability_status": "available" if is_publicly_orderable else "unavailable",
        "is_featured": product.is_featured is True,
        "published_at": _json_value(product.approved_at or product.created_at),
    }
    if variants is not None:
        row["variants"] = [
            public_variant_response(variant)
            for variant in variants
            if variant.deleted_at is None and variant.is_active is not False
        ]
    return row


def _public_upload_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("url", "image_url", "imageUrl", "path", "src"):
            normalized = _public_upload_url(value.get(key))
            if normalized:
                return normalized
        return None
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/")
    if not raw:
        return None
    lowered = raw.lower()
    for marker in ("/api/uploads/", "/uploads/", "uploads/", "backend/data/uploads/"):
        index = lowered.find(marker)
        if index >= 0:
            relative = raw[index + len(marker) :].lstrip("/")
            return f"/uploads/{relative}" if relative else None
    if lowered.startswith(("http://", "https://")):
        # Keep only the configured public R2 hostname as an absolute URL.
        # Legacy Render upload URLs are intentionally normalized to /uploads/.
        configured_public_host = urlparse(
            str(get_settings().r2_public_base_url or "")
        ).hostname
        parsed = urlparse(raw)
        if (
            parsed.scheme == "https"
            and configured_public_host
            and parsed.hostname
            and parsed.hostname.lower() == configured_public_host.lower()
        ):
            return raw
        return None
    if lowered.startswith(("data:", "javascript:")):
        return None
    return f"/uploads/{raw.lstrip('/')}"


async def build_public_product_rows(
    session: AsyncSession,
    products: list[Product],
    *,
    include_variants: bool = False,
) -> list[dict[str, Any]]:
    if not products:
        return []
    category_ids = {product.category_id for product in products if product.category_id}
    brand_ids = {product.brand_id for product in products if product.brand_id}
    partner_ids = {product.partner_id for product in products if product.partner_id}
    product_ids = [product.id for product in products]

    categories: dict[uuid.UUID, Category] = {}
    brands: dict[uuid.UUID, Brand] = {}
    storefronts: dict[uuid.UUID, Any] = {}
    variants_by_product: dict[uuid.UUID, list[ProductVariant]] = {}

    if category_ids:
        result = await session.execute(select(Category).where(Category.id.in_(category_ids)))
        categories = {row.id: row for row in result.scalars()}
    if brand_ids:
        result = await session.execute(select(Brand).where(Brand.id.in_(brand_ids)))
        brands = {row.id: row for row in result.scalars()}
    if partner_ids and "partner_storefronts" in MODEL_BY_TABLE:
        model = MODEL_BY_TABLE["partner_storefronts"]
        clauses = []
        if "partner_id" in model.__table__.c:
            clauses.append(model.partner_id.in_(partner_ids))
        if "user_id" in model.__table__.c:
            clauses.append(model.user_id.in_(partner_ids))
        result = await session.execute(
            select(model).where(or_(*clauses), model.deleted_at.is_(None), model.is_active.is_(True))
        )
        for storefront in result.scalars():
            for key in (getattr(storefront, "partner_id", None), getattr(storefront, "user_id", None)):
                if key:
                    storefronts[key] = storefront
    if include_variants:
        result = await session.execute(
            select(ProductVariant)
            .where(
                ProductVariant.product_id.in_(product_ids),
                ProductVariant.deleted_at.is_(None),
                ProductVariant.is_active.is_(True),
            )
            .order_by(ProductVariant.sort_order.asc(), ProductVariant.created_at.asc())
        )
        for variant in result.scalars():
            variants_by_product.setdefault(variant.product_id, []).append(variant)

    return [
        public_product_response(
            product,
            category=categories.get(product.category_id),
            brand=brands.get(product.brand_id),
            merchant=storefronts.get(product.partner_id),
            variants=variants_by_product.get(product.id) if include_variants else None,
        )
        for product in products
        if is_public_product(product)
    ]


def validate_public_product_or_404(product: Product | None) -> Product:
    if product is None or not is_public_product(product):
        raise HTTPException(status_code=404, detail="product_not_found")
    return product


def assert_merchant_product_payload_allowed(raw: dict[str, Any]) -> None:
    for field in raw:
        if field in MERCHANT_BLOCKED_PRODUCT_FIELDS:
            raise HTTPException(status_code=403, detail=f"merchant_product_field_denied:{field}")


def normalize_product_mutation_values(values: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    normalized = dict(values)
    if "name" in normalized or not partial:
        name = str(normalized.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail={"code": "product_name_required", "message": "Product name is required"})
        if len(name) < 2 or len(name) > 500 or _CONTROL_CHARS.search(name):
            raise HTTPException(status_code=422, detail={"code": "invalid_product_name", "message": "Product name is invalid"})
        normalized["name"] = name
    for key in ("name_en", "description", "rich_description", "meta_title", "meta_description", "promotional_title"):
        if key in normalized and normalized[key] is not None:
            text = str(normalized[key]).strip()
            if _CONTROL_CHARS.search(text) or "<script" in text.lower():
                raise HTTPException(status_code=422, detail={"code": f"invalid_{key}", "message": "Product text contains unsupported content"})
            max_length = 500 if key in {"name_en", "meta_title", "promotional_title"} else 20000
            if len(text) > max_length:
                raise HTTPException(status_code=422, detail={"code": f"invalid_{key}", "message": "Product text is too long"})
            normalized[key] = text or None
    if "sku" in normalized and normalized["sku"] is not None:
        sku = str(normalized["sku"]).strip().upper()
        if not sku:
            normalized["sku"] = None
        elif not _SKU_PATTERN.match(sku):
            raise HTTPException(status_code=422, detail={"code": "invalid_sku", "message": "SKU is invalid"})
        else:
            normalized["sku"] = sku
    for key in ("price", "original_price"):
        if key in normalized and normalized[key] is not None:
            try:
                amount = Decimal(str(normalized[key]))
            except Exception:
                raise HTTPException(status_code=422, detail={"code": f"invalid_{key}", "message": "Product price is invalid"})
            if amount < 0 or amount > Decimal("999999999999.99"):
                raise HTTPException(status_code=422, detail={"code": f"invalid_{key}", "message": "Product price is invalid"})
            normalized[key] = amount.quantize(Decimal("0.01"))
    if "price" not in normalized and not partial:
        normalized["price"] = Decimal("0.00")
    if "price" in normalized and "original_price" in normalized and normalized.get("original_price") is not None:
        if normalized["original_price"] < normalized.get("price", Decimal("0")):
            raise HTTPException(status_code=422, detail={"code": "invalid_original_price", "message": "Original price cannot be lower than price"})
    for key in ("stock_quantity", "min_stock_quantity"):
        if key in normalized and normalized[key] is not None:
            try:
                count = int(normalized[key])
            except Exception:
                raise HTTPException(status_code=422, detail={"code": f"invalid_{key}", "message": "Product stock is invalid"})
            if count < 0 or count > 1_000_000:
                raise HTTPException(status_code=422, detail={"code": f"invalid_{key}", "message": "Product stock is invalid"})
            normalized[key] = count
    if "images" in normalized:
        images = normalized["images"] or []
        if not isinstance(images, list) or len(images) > 20:
            raise HTTPException(status_code=422, detail={"code": "invalid_images", "message": "Product images payload is invalid"})
        for image in images:
            value = image.get("url") if isinstance(image, dict) else image
            if isinstance(value, str) and value.strip().lower().startswith(("javascript:", "data:")):
                raise HTTPException(status_code=422, detail={"code": "invalid_images", "message": "Product images payload is invalid"})
    return normalized


def apply_merchant_product_server_defaults(values: dict[str, Any], *, existing: Product | None = None) -> dict[str, Any]:
    clean = dict(values)
    clean["approval_status"] = "pending"
    clean["approval_notes"] = None
    clean["approved_by"] = None
    clean["approved_at"] = None
    clean["is_featured"] = False
    if existing is None or any(field in values for field in MERCHANT_SENSITIVE_PRODUCT_FIELDS):
        clean["is_active"] = False
    return clean
