from __future__ import annotations

import hashlib
import json
import re
import uuid
from io import BytesIO
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...dependencies import current_user, optional_user, require_staff, user_roles
from ...config import get_settings
from ...models import MODEL_BY_TABLE
from ...models.domain import (
    Brand,
    Category,
    FileAsset,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    User,
    UserCart,
    Wishlist,
)
from ...repositories.resources import serialize_record
from ...services.catalog_policy import (
    apply_merchant_product_server_defaults,
    assert_merchant_product_payload_allowed,
    build_public_product_rows,
    is_public_product,
    new_product_clause,
    first_safe_display_text,
    normalize_product_mutation_values,
    public_product_base_clauses,
    public_product_clauses,
    public_main_storefront_response,
    public_product_response,
    public_storefront_response,
    validate_public_product_or_404,
    _public_upload_url,
)
from ...services.financial_calculator import (
    calculate_checkout_financials,
    line_total,
    money,
    serialize_local_shopping_requests,
    unit_price,
)
from ...services.merchant_order_scope import merchant_order_detail, merchant_order_list
from ...services.public_read_cache import cache_key, public_read_cache
from ...services.product_identifier import decode_compact_uuid
from ...services.commerce_rules import (
    eligible_line,
    parse_strict_quantity,
    validate_customer_checkout_address,
    validate_shipping_address,
)
from ...services.payment_methods import validate_payment_method_for_checkout
from ...services.staff_permissions import require_staff_permission
from ...services.order_state_machine import assert_allowed_transition, assert_delivery_proof, normalize_status
from ...services.notification_service import NotificationPayload, NotificationService
from ...storage import FileStorage


router = APIRouter(tags=["commerce"])
storage = FileStorage()


IDEMPOTENCY_RESPONSE_INTERNAL_FIELDS = {
    "idempotency_actor_id",
    "idempotency_endpoint",
    "idempotency_request_hash",
}


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"invalid_uuid:{field}")


def _money(value: Any) -> Decimal:
    try:
        return max(Decimal(str(value or 0)), Decimal("0"))
    except Exception:
        return Decimal("0")


def _line_original_unit(product: Product, variant: ProductVariant | None, sale_unit: Decimal) -> Decimal:
    raw = variant.original_price if variant is not None and variant.original_price is not None else product.original_price
    try:
        original = money(raw) if raw is not None else sale_unit
    except HTTPException:
        return sale_unit
    return original if original > sale_unit else sale_unit


def _normalize_idempotency_key(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    if len(key) > 120:
        raise HTTPException(status_code=400, detail="idempotency_key_too_long")
    return key


def _request_hash(body: Any) -> str:
    payload = dict(body) if isinstance(body, dict) else {"body": body}
    payload.pop("idempotencyKey", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_placeholder_product_image(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().replace("\\", "/").lower()
    return normalized.endswith("/uploads/placeholders/product-default.jpg") or normalized.endswith(
        "uploads/placeholders/product-default.jpg"
    )


def _resolve_manage_product_image_urls(row: dict[str, Any]) -> dict[str, Any]:
    def _resolve(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return value
        resolved = _public_upload_url(text)
        if resolved is not None:
            return resolved
        if text.startswith(("http://", "https://")):
            return text
        return value

    for key in ("image_url", "imageUrl"):
        if key in row and row.get(key) is not None:
            row[key] = _resolve(row.get(key))
    images = row.get("images")
    if isinstance(images, list):
        resolved_images: list[Any] = []
        for image in images:
            if isinstance(image, str):
                resolved_images.append(_resolve(image))
                continue
            if isinstance(image, dict):
                copy = dict(image)
                for nested_key in ("url", "image_url", "imageUrl", "path", "src"):
                    if nested_key in copy and copy.get(nested_key) is not None:
                        copy[nested_key] = _resolve(copy.get(nested_key))
                resolved_images.append(copy)
                continue
            resolved_images.append(image)
        row["images"] = resolved_images
    return row


def _serialize_manage_product(product: Product) -> dict[str, Any]:
    row = serialize_record(product)
    _hide_placeholder_product_images(row)
    _resolve_manage_product_image_urls(row)
    display = first_safe_display_text(
        product.name,
        product.name_en,
        product.promotional_title,
        product.meta_title,
    )
    if display:
        row["display_name"] = display
    return row


async def _serialize_manage_products(
    session: AsyncSession, products: list[Product]
) -> list[dict[str, Any]]:
    if not products:
        return []
    brand_ids = {product.brand_id for product in products if product.brand_id}
    supplier_ids = {product.supplier_id for product in products if product.supplier_id}
    brands_by_id: dict[uuid.UUID, Brand] = {}
    suppliers_by_id: dict[uuid.UUID, Any] = {}
    if brand_ids:
        result = await session.execute(select(Brand).where(Brand.id.in_(brand_ids)))
        brands_by_id = {row.id: row for row in result.scalars()}
    supplier_model = MODEL_BY_TABLE.get("suppliers")
    if supplier_ids and supplier_model is not None:
        result = await session.execute(
            select(supplier_model).where(supplier_model.id.in_(supplier_ids))
        )
        suppliers_by_id = {row.id: row for row in result.scalars()}

    rows: list[dict[str, Any]] = []
    for product in products:
        row = _serialize_manage_product(product)
        brand = brands_by_id.get(product.brand_id) if product.brand_id else None
        if brand is not None:
            row["brand_name"] = brand.name
            if brand.name_en:
                row["brand_name_en"] = brand.name_en
        supplier = suppliers_by_id.get(product.supplier_id) if product.supplier_id else None
        if supplier is not None:
            supplier_name = getattr(supplier, "name", None) or getattr(
                supplier, "business_name", None
            )
            if supplier_name:
                row["supplier_name"] = str(supplier_name).strip()
        rows.append(row)
    return rows


def _require_manage_catalog_roles(roles: set[str]) -> None:
    if not roles.intersection({"admin", "manager", "partner", "staff", "employee", "logistics"}):
        raise HTTPException(status_code=403, detail="insufficient_permissions")


async def _apply_brand_supplier_name_refs(
    session: AsyncSession, body: dict[str, Any], values: dict[str, Any]
) -> None:
    if not values.get("brand_id"):
        brand_name = str(body.get("brandName") or body.get("brand_name") or "").strip()
        if brand_name:
            existing = await session.execute(
                select(Brand)
                .where(
                    Brand.deleted_at.is_(None),
                    func.lower(func.trim(Brand.name)) == brand_name.lower(),
                )
                .limit(1)
            )
            brand = existing.scalar_one_or_none()
            if brand is None:
                brand = Brand(name=brand_name, is_active=True)
                session.add(brand)
                await session.flush()
            values["brand_id"] = brand.id
    if not values.get("supplier_id"):
        supplier_name = str(
            body.get("supplierName") or body.get("supplier_name") or ""
        ).strip()
        if not supplier_name:
            return
        supplier_model = MODEL_BY_TABLE.get("suppliers")
        if supplier_model is None:
            return
        name_column = getattr(supplier_model, "name", None)
        if name_column is None:
            return
        existing = await session.execute(
            select(supplier_model)
            .where(
                supplier_model.deleted_at.is_(None),
                func.lower(func.trim(name_column)) == supplier_name.lower(),
            )
            .limit(1)
        )
        supplier = existing.scalar_one_or_none()
        if supplier is None:
            supplier = supplier_model(name=supplier_name, is_active=True)
            session.add(supplier)
            await session.flush()
        values["supplier_id"] = supplier.id


def _hide_placeholder_product_images(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("image_url", "imageUrl"):
        if _is_placeholder_product_image(row.get(key)):
            row[key] = None
    images = row.get("images")
    if isinstance(images, list):
        sanitized_images = []
        for image in images:
            if _is_placeholder_product_image(image):
                continue
            if isinstance(image, dict) and any(
                _is_placeholder_product_image(image.get(key))
                for key in ("url", "image_url", "imageUrl", "path", "src")
            ):
                continue
            sanitized_images.append(image)
        row["images"] = sanitized_images
    return row


PRODUCT_IMAGE_REQUIRED_DETAIL = {
    "code": "PRODUCT_IMAGE_REQUIRED",
    "message": "لا يمكن نشر المنتج قبل إضافة صورة رئيسية صالحة.",
}

PRODUCT_CONTENT_REQUIRED_DETAIL = {
    "code": "PRODUCT_CONTENT_REQUIRED",
    "message": "لا يمكن نشر المنتج قبل إضافة اسم تجاري واضح وصالح.",
}


_INTERNAL_VISIBLE_TEXT_PATTERNS = (
    re.compile(r"\bCODEX\b|CODEX_", re.IGNORECASE),
    re.compile(r"\bE2E\b|E2E_", re.IGNORECASE),
    re.compile(r"\bTEST\b|TEST_", re.IGNORECASE),
    re.compile(r"\bMOCK\b|\bDUMMY\b|\bSAMPLE\b|\bFIXTURE\b|RUN_ID", re.IGNORECASE),
    re.compile(r"^(Category|Store)\s+[0-9a-f_-]{5,}$", re.IGNORECASE),
    re.compile(r"^Imported product\b", re.IGNORECASE),
    re.compile(r"^Unknown product\b|^Unknown item\b", re.IGNORECASE),
    re.compile(r"^Product\s+[0-9a-f_-]{5,}$", re.IGNORECASE),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE),
)


def _safe_public_display_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text_value = value.strip()
    if not text_value:
        return False
    return not any(pattern.search(text_value) for pattern in _INTERNAL_VISIBLE_TEXT_PATTERNS)


def _row_has_safe_public_product_text(row: dict[str, Any]) -> bool:
    return _safe_public_display_text(row.get("name")) or _safe_public_display_text(row.get("name_en"))


def _product_has_safe_public_text(product: Product) -> bool:
    return _safe_public_display_text(product.name) or _safe_public_display_text(product.name_en)


def _product_image_upload_path(value: Any, upload_dir: Path | None = None) -> Path | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().strip('"').strip("'")
    if not raw or _is_placeholder_product_image(raw):
        return None
    normalized = raw.replace("\\", "/")
    lower = normalized.lower()
    blocked_storage_host = "supa" + "base.co"
    blocked_storage_path = "/".join(("storage", "v1"))
    if blocked_storage_host in lower or blocked_storage_path in lower:
        return None
    if lower.startswith(("http://", "https://")):
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower()
        settings = get_settings()
        allowed_hosts = {
            urlparse(settings.api_base_url).hostname,
            urlparse(settings.app_public_url).hostname,
            "testserver",
        }
        if settings.app_env != "production":
            allowed_hosts.update({"127.0.0.1", "localhost", "10.0.2.2"})
        if host not in {item for item in allowed_hosts if item}:
            return None
        path = unquote(parsed.path or "")
    elif urlparse(normalized).scheme or (len(normalized) > 2 and normalized[1:3] == ":/"):
        return None
    else:
        path = normalized
    for prefix in ("/uploads/", "uploads/", "/api/uploads/", "backend/data/uploads/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    path = path.lstrip("/")
    if not path:
        return None
    relative = Path(path)
    if ".." in relative.parts:
        return None
    base = (upload_dir or get_settings().resolved_upload_dir).resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def _product_storage_key(value: Any) -> str | None:
    """Convert a product image URL into the storage key used by FileAsset."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().strip('"').strip("'")
    parsed = urlparse(raw)
    path = unquote(parsed.path or raw).replace("\\", "/").lstrip("/")
    if "/uploads/" in path:
        path = path.split("/uploads/", 1)[1]
    elif path.startswith("uploads/"):
        path = path[len("uploads/"):]
    if not path or ".." in Path(path).parts:
        return None
    suffix = Path(path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}:
        return None
    return path


def _product_image_values(product: Product, variants: list[ProductVariant]) -> set[str]:
    values: set[str] = set()
    rows: list[Any] = [product, *variants]
    for row in rows:
        for field in ("image_url", "imageUrl"):
            key = _product_storage_key(getattr(row, field, None))
            if key:
                values.add(key)
        images = getattr(row, "images", None)
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict):
                    image = image.get("url") or image.get("image_url") or image.get("imageUrl") or image.get("src")
                key = _product_storage_key(image)
                if key:
                    values.add(key)
    return values


async def _delete_product_file_assets(
    session: AsyncSession,
    *,
    product: Product,
    variants: list[ProductVariant],
    actor: User,
) -> int:
    """Delete unshared product image assets from local storage or R2.

    Product images are uploaded before the product is saved, so older rows may
    not have entity_id metadata. Matching by the normalized storage key keeps
    deletion working for those rows while the active-product reference check
    prevents deleting a shared image.
    """
    keys = _product_image_values(product, variants)
    if not keys:
        return 0
    await session.flush()
    other_products = list(
        (
            await session.execute(
                select(Product).where(Product.deleted_at.is_(None), Product.id != product.id)
            )
        ).scalars()
    )
    other_variants = list(
        (
            await session.execute(
                select(ProductVariant).where(ProductVariant.deleted_at.is_(None), ProductVariant.product_id != product.id)
            )
        ).scalars()
    )
    referenced_elsewhere: set[str] = set()
    for row in [*other_products, *other_variants]:
        for field in ("image_url", "imageUrl"):
            key = _product_storage_key(getattr(row, field, None))
            if key:
                referenced_elsewhere.add(key)
        images = getattr(row, "images", None)
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict):
                    image = image.get("url") or image.get("image_url") or image.get("imageUrl") or image.get("src")
                key = _product_storage_key(image)
                if key:
                    referenced_elsewhere.add(key)
    assets = list(
        (
            await session.execute(
                select(FileAsset).where(
                    FileAsset.deleted_at.is_(None),
                    FileAsset.status == "available",
                    FileAsset.storage_key.in_(keys),
                )
            )
        ).scalars()
    )
    assets_by_key = {
        str(asset.storage_key).replace("\\", "/").lstrip("/"): asset
        for asset in assets
    }
    configured_provider = (
        "cloudflare_r2"
        if str(get_settings().storage_provider or "").strip().lower() == "r2"
        else "local_uploads"
    )
    removed = 0
    now = datetime.now(timezone.utc)
    for key in sorted(keys):
        normalized_key = str(key).replace("\\", "/").lstrip("/")
        if normalized_key in referenced_elsewhere:
            continue
        asset = assets_by_key.get(normalized_key)
        provider = str(getattr(asset, "storage_provider", "") or configured_provider)
        # Legacy and direct R2 uploads may have a product URL but no FileAsset
        # row. Delete the object by its normalized key as well, while retaining
        # the active-product reference guard above.
        storage.delete_relative(normalized_key, storage_provider=provider)
        if asset is not None:
            asset.deleted_at = now
            asset.deleted_by = actor.id
            asset.status = "deleted"
        removed += 1
    return removed


def _is_configured_r2_public_image_ref(value: Any) -> bool:
    """Accept a public image that was produced by the configured R2 origin.

    R2 uploads are durable objects, so they do not have to be present in the
    Render upload directory. The upload endpoint is the trust boundary; this
    check only accepts the exact HTTPS host configured for public R2 objects
    and a safe image path.
    """
    if not isinstance(value, str):
        return False
    raw = value.strip().strip('"').strip("'")
    if not raw.lower().startswith("https://"):
        return False
    configured = urlparse(str(get_settings().r2_public_base_url or "").strip())
    parsed = urlparse(raw)
    if (
        configured.scheme != "https"
        or not configured.hostname
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() != configured.hostname.lower()
    ):
        return False
    relative = unquote(parsed.path or "").lstrip("/")
    if not relative or ".." in Path(relative).parts:
        return False
    return Path(relative).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}


def _image_magic_mime(path: Path) -> str | None:
    try:
        header = path.read_bytes()[:16]
    except OSError:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _valid_product_primary_image_ref(value: Any, upload_dir: Path | None = None) -> bool:
    if _is_configured_r2_public_image_ref(value):
        return True
    path = _product_image_upload_path(value, upload_dir=upload_dir)
    if path is None or not path.is_file():
        return False
    try:
        if path.stat().st_size <= 0:
            return False
    except OSError:
        return False
    extension = path.suffix.lower().lstrip(".")
    expected_by_extension = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }
    expected = expected_by_extension.get(extension)
    return expected is not None and _image_magic_mime(path) == expected


def _row_has_valid_public_primary_image(row: dict[str, Any], upload_dir: Path | None = None) -> bool:
    primary = row.get("image_url") or row.get("imageUrl")
    if _valid_product_primary_image_ref(primary, upload_dir=upload_dir):
        return True
    images = row.get("images")
    if isinstance(images, list):
        return any(_valid_product_primary_image_ref(image, upload_dir=upload_dir) for image in images)
    return False


def _product_has_valid_primary_image(product: Product, upload_dir: Path | None = None) -> bool:
    if _valid_product_primary_image_ref(product.image_url, upload_dir=upload_dir):
        return True
    return any(
        _valid_product_primary_image_ref(image, upload_dir=upload_dir)
        for image in (product.images or [])
    )


def _normalize_public_product_images(row: dict[str, Any]) -> dict[str, Any]:
    """Return one canonical image list and derive the primary image from it."""
    candidates: list[Any] = []
    candidates.extend((row.get("image_url"), row.get("imageUrl")))
    images = row.get("images")
    if isinstance(images, list):
        candidates.extend(images)
    normalized: list[str] = []
    for candidate in candidates:
        value = _public_upload_url(candidate)
        if value and value not in normalized:
            normalized.append(value)
    primary = normalized[0] if normalized else None
    row["image_url"] = primary
    row["imageUrl"] = primary
    row["images"] = normalized
    row["primary_image"] = primary
    return row


def _public_visibility_requires_valid_image(product: Product) -> bool:
    is_active = product.is_active if product.is_active is not None else True
    approval_status = (product.approval_status or "approved").lower()
    is_featured = bool(product.is_featured)
    has_offer = bool(str(product.promotional_title or "").strip())
    return (bool(is_active) and approval_status in {"approved", "active", "published"}) or is_featured or has_offer


def _ensure_product_image_for_public_visibility(product: Product) -> None:
    if _public_visibility_requires_valid_image(product) and not _product_has_valid_primary_image(product):
        raise HTTPException(status_code=422, detail=PRODUCT_IMAGE_REQUIRED_DETAIL)


def _ensure_product_content_for_public_visibility(product: Product) -> None:
    if _public_visibility_requires_valid_image(product) and not _product_has_safe_public_text(product):
        raise HTTPException(status_code=422, detail=PRODUCT_CONTENT_REQUIRED_DETAIL)


def _enforce_product_public_quality(product: Product, roles: set[str]) -> None:
    if get_settings().fixtures_enabled and roles.intersection({"admin", "manager"}):
        return
    _ensure_product_image_for_public_visibility(product)
    _ensure_product_content_for_public_visibility(product)


async def _advisory_xact_lock(session: AsyncSession, scope: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:scope)::bigint)"),
        {"scope": scope},
    )


def _serialize_order(order: Order, *, idempotency_replayed: bool | None = None) -> dict[str, Any]:
    row = serialize_record(order)
    for key in IDEMPOTENCY_RESPONSE_INTERNAL_FIELDS:
        row.pop(key, None)
    if idempotency_replayed is not None:
        row["idempotency_replayed"] = idempotency_replayed
    return row


def _idempotency_replay_response(
    order: Order,
    *,
    actor_id: uuid.UUID,
    endpoint: str,
    request_hash: str,
) -> dict[str, Any]:
    extra = order.extra_data or {}
    stored_actor = extra.get("idempotency_actor_id")
    if stored_actor and stored_actor != str(actor_id):
        raise HTTPException(status_code=409, detail="idempotency_key_conflict")
    if not stored_actor and order.user_id != actor_id and order.created_by != actor_id:
        raise HTTPException(status_code=409, detail="idempotency_key_conflict")
    stored_endpoint = extra.get("idempotency_endpoint")
    if stored_endpoint and stored_endpoint != endpoint:
        raise HTTPException(status_code=409, detail="idempotency_key_conflict")
    stored_hash = extra.get("idempotency_request_hash")
    if stored_hash and stored_hash != request_hash:
        raise HTTPException(status_code=409, detail="idempotency_key_conflict")
    return _serialize_order(order, idempotency_replayed=True)


async def _product_payload(session: AsyncSession, product: Product) -> dict[str, Any]:
    category = None
    brand = None
    if product.category_id:
        category = await session.get(Category, product.category_id)
    if product.brand_id:
        brand = await session.get(Brand, product.brand_id)
    row = public_product_response(product, category=category, brand=brand)
    if category:
        row["category_name"] = category.name
        row["category_slug"] = category.slug
    if brand:
        row["brand_name"] = brand.name
    return row


async def _product_payloads(
    session: AsyncSession,
    products: list[Product],
    *,
    public: bool = True,
) -> list[dict[str, Any]]:
    if not products:
        return []
    product_ids = [product.id for product in products]
    category_ids = {product.category_id for product in products if product.category_id}
    brand_ids = {product.brand_id for product in products if product.brand_id}
    categories_by_id: dict[uuid.UUID, Category] = {}
    brands_by_id: dict[uuid.UUID, Brand] = {}
    variants_by_product_id: dict[uuid.UUID, list[ProductVariant]] = {}
    review_stats_by_product_id: dict[uuid.UUID, tuple[float, int]] = {}
    if category_ids:
        result = await session.execute(select(Category).where(Category.id.in_(category_ids)))
        categories_by_id = {row.id: row for row in result.scalars()}
    if brand_ids:
        result = await session.execute(select(Brand).where(Brand.id.in_(brand_ids)))
        brands_by_id = {row.id: row for row in result.scalars()}
    if product_ids:
        result = await session.execute(
            select(ProductVariant)
            .where(
                ProductVariant.product_id.in_(product_ids),
                ProductVariant.deleted_at.is_(None),
                ProductVariant.is_active.is_(True),
            )
            .order_by(ProductVariant.sort_order)
        )
        for variant in result.scalars():
            variants_by_product_id.setdefault(variant.product_id, []).append(variant)
    review_model = MODEL_BY_TABLE.get("product_reviews")
    if review_model is not None and "rating" in review_model.__table__.c:
        review_columns = review_model.__table__.c
        review_clauses = [review_columns.product_id.in_(product_ids)]
        if "deleted_at" in review_columns:
            review_clauses.append(review_columns.deleted_at.is_(None))
        if "status" in review_columns:
            review_clauses.append(review_columns.status.in_(
                ("approved", "active", "published", "visible", "live")
            ))
        if "is_approved" in review_columns:
            review_clauses.append(review_columns.is_approved.is_(True))
        review_result = await session.execute(
            select(
                review_columns.product_id,
                func.avg(review_columns.rating),
                func.count(review_columns.rating),
            )
            .where(*review_clauses)
            .group_by(review_columns.product_id)
        )
        review_stats_by_product_id = {
            product_id: (round(float(average or 0), 2), int(count or 0))
            for product_id, average, count in review_result.all()
        }

    rows: list[dict[str, Any]] = []
    for product in products:
        category = categories_by_id.get(product.category_id)
        brand = brands_by_id.get(product.brand_id)
        if public:
            row = public_product_response(
                product,
                category=category,
                brand=brand,
                variants=variants_by_product_id.get(product.id),
            )
            if category:
                row["category_name"] = category.name
                row["category_slug"] = category.slug
            if brand:
                row["brand_name"] = brand.name
        else:
            row = serialize_record(product)
            _hide_placeholder_product_images(row)
            if product.image_url and not row.get("images"):
                row["images"] = [product.image_url]
            _normalize_public_product_images(row)
            if category:
                row["category"] = serialize_record(category)
                row["category_name"] = category.name
                row["category_slug"] = category.slug
            if brand:
                row["brand"] = serialize_record(brand)
                row["brand_name"] = brand.name
            row["variants"] = [
                serialize_record(variant)
                for variant in variants_by_product_id.get(product.id, [])
            ]
        rating_average, rating_count = review_stats_by_product_id.get(product.id, (0.0, 0))
        row["rating_average"] = rating_average
        row["rating_count"] = rating_count
        row["reviews_count"] = rating_count
        rows.append(row)
    return rows


def _public_products_statement():
    return select(Product).where(*public_product_clauses(Product))


async def _category_with_descendant_ids(
    session: AsyncSession, category_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    if not category_ids:
        return set()
    # Resolve the subtree in PostgreSQL instead of loading every active
    # category into Python for every filtered catalogue request. UNION (rather
    # than UNION ALL) also makes the query safe if legacy data contains a
    # malformed cycle.
    category_tree = (
        select(Category.id)
        .where(
            Category.id.in_(category_ids),
            Category.deleted_at.is_(None),
            Category.is_active.is_(True),
        )
        .cte("public_category_tree", recursive=True)
    )
    category_tree = category_tree.union(
        select(Category.id)
        .join(category_tree, Category.parent_id == category_tree.c.id)
        .where(
            Category.deleted_at.is_(None),
            Category.is_active.is_(True),
        )
    )
    result = await session.execute(select(category_tree.c.id))
    return set(result.scalars())


async def _apply_public_product_filters(
    session: AsyncSession,
    statement,
    *,
    featured: bool = False,
    on_sale: bool = False,
    new_only: bool = False,
    search: str | None = None,
    category_id: str | None = None,
    category_ids: str | None = None,
    category_slug: str | None = None,
    category_name: str | None = None,
    brand: str | None = None,
    brand_id: str | None = None,
    brand_slug: str | None = None,
    partner_id: str | None = None,
    supplier_id: str | None = None,
    main_store_only: bool = False,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
):
    if featured:
        statement = statement.where(Product.is_featured.is_(True))
    if on_sale:
        statement = statement.where(Product.original_price.is_not(None), Product.original_price > Product.price)
    if new_only:
        statement = statement.where(new_product_clause(Product))
    if search and search.strip():
        term = f"%{search.strip()}%"
        statement = statement.where(
            or_(
                Product.name.ilike(term),
                Product.name_en.ilike(term),
                Product.description.ilike(term),
                Product.sku.ilike(term),
                Product.short_code.ilike(term),
            )
        )
    selected_category_ids: set[uuid.UUID] = set()
    if category_id:
        selected_category_ids.add(_uuid(category_id, "categoryId"))
    if category_ids:
        for value in str(category_ids).split(","):
            if value.strip():
                selected_category_ids.add(_uuid(value.strip(), "categoryIds"))
    if category_slug and category_slug.lower() not in {"all", "products", "all-products"}:
        category = await session.execute(select(Category.id).where(Category.slug == category_slug))
        resolved = category.scalar_one_or_none()
        if resolved is None:
            return statement.where(False)
        selected_category_ids.add(resolved)
    if category_name and category_name.lower() not in {"all", "products", "all-products"}:
        category = await session.execute(
            select(Category.id).where(or_(Category.name == category_name, Category.name_en == category_name))
        )
        resolved = category.scalar_one_or_none()
        if resolved is None:
            return statement.where(False)
        selected_category_ids.add(resolved)
    if selected_category_ids:
        selected_category_ids = await _category_with_descendant_ids(
            session, selected_category_ids
        )
        statement = statement.where(Product.category_id.in_(selected_category_ids))
    if brand_id:
        statement = statement.where(Product.brand_id == _uuid(brand_id, "brandId"))
    if brand_slug or brand:
        lookup = brand_slug or brand
        found_brand = await session.execute(
            select(Brand.id).where(or_(Brand.slug == lookup, Brand.name == lookup, Brand.name_en == lookup))
        )
        resolved = found_brand.scalar_one_or_none()
        if resolved is None:
            return statement.where(False)
        statement = statement.where(Product.brand_id == resolved)
    if partner_id:
        statement = statement.where(Product.partner_id == _uuid(partner_id, "partnerId"))
    if supplier_id:
        statement = statement.where(Product.supplier_id == _uuid(supplier_id, "supplierId"))
    if main_store_only:
        statement = statement.where(Product.partner_id.is_(None))
        local_model = MODEL_BY_TABLE.get("local_merchants")
        if local_model is not None:
            local_result = await session.execute(
                select(local_model.id).where(
                    local_model.deleted_at.is_(None),
                    local_model.is_active.is_(True),
                )
            )
            local_ids = list(local_result.scalars())
            if local_ids:
                statement = statement.where(
                    or_(Product.supplier_id.is_(None), ~Product.supplier_id.in_(local_ids))
                )
    if min_price is not None:
        statement = statement.where(Product.price >= min_price)
    if max_price is not None:
        statement = statement.where(Product.price <= max_price)
    return statement


def _apply_public_product_sort(statement, sort: str):
    if sort in {"priceLow", "price_asc", "price-asc"}:
        return statement.order_by(Product.price.asc(), Product.created_at.desc())
    if sort in {"priceHigh", "price_desc", "price-desc"}:
        return statement.order_by(Product.price.desc(), Product.created_at.desc())
    if sort in {"name", "name_asc", "name-asc"}:
        return statement.order_by(Product.name.asc())
    if sort in {"discount", "best_discount"}:
        return statement.order_by((Product.original_price - Product.price).desc().nullslast(), Product.created_at.desc())
    return statement.order_by(Product.created_at.desc())


async def _catalog_products_page(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    offset: int | None,
    sort: str,
    include_total: bool = True,
    **filters: Any,
) -> dict[str, Any]:
    key = cache_key(
        "catalog-products",
        page=page,
        page_size=page_size,
        offset=offset,
        sort=sort,
        include_total=include_total,
        filters=filters,
    )
    return await public_read_cache.get_or_set(
        key,
        lambda: _catalog_products_page_uncached(
            session,
            page=page,
            page_size=page_size,
            offset=offset,
            sort=sort,
            include_total=include_total,
            **filters,
        ),
    )


async def _catalog_products_page_uncached(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    offset: int | None,
    sort: str,
    include_total: bool = True,
    **filters: Any,
) -> dict[str, Any]:
    safe_page_size = min(max(int(page_size or 20), 1), 5000)
    safe_page = max(int(page or 1), 1)
    safe_offset = max(int(offset), 0) if offset is not None else (safe_page - 1) * safe_page_size
    statement = await _apply_public_product_filters(session, _public_products_statement(), **filters)
    total: int | None = None
    if include_total:
        # Count only the indexed primary-key column.  Counting a subquery that
        # selects every Product column makes PostgreSQL carry a much wider
        # intermediate row set than the catalogue needs.
        total_statement = statement.with_only_columns(
            func.count(Product.id),
            maintain_column_froms=True,
        ).order_by(None)
        total_result = await session.execute(total_statement)
        total = int(total_result.scalar_one() or 0)
    result = await session.execute(_apply_public_product_sort(statement, sort).offset(safe_offset).limit(safe_page_size))
    items = await _product_payloads(session, list(result.scalars()))
    total_pages = (total + safe_page_size - 1) // safe_page_size if total is not None and total else 0
    current_page = safe_offset // safe_page_size + 1
    return {
        "items": items,
        "data": items,
        "total": total,
        "page": current_page,
        "page_size": safe_page_size,
        "limit": safe_page_size,
        "total_pages": total_pages,
        "has_next": (
            safe_offset + len(items) < total
            if total is not None
            else len(items) >= safe_page_size
        ),
        "has_previous": safe_offset > 0,
    }


@router.get("/categories")
async def categories(limit: int = Query(100, ge=1, le=5000), session: AsyncSession = Depends(get_session)):
    key = cache_key("catalog-categories", limit=limit)
    return await public_read_cache.get_or_set(key, lambda: _categories_uncached(limit=limit, session=session))


async def _categories_uncached(limit: int, session: AsyncSession) -> list[dict[str, Any]]:
    category_result = await session.execute(
        select(Category)
        .where(Category.deleted_at.is_(None), Category.is_active.is_(True))
        .order_by(Category.sort_order, Category.name)
        .limit(5000)
    )
    all_categories = list(category_result.scalars())
    if not all_categories:
        return []
    category_ids = [category.id for category in all_categories]
    product_count_result = await session.execute(
        select(Product.category_id, func.count(Product.id))
        .where(Product.category_id.in_(category_ids), *public_product_clauses(Product))
        .group_by(Product.category_id)
    )
    direct_counts = {
        category_id: int(count or 0)
        for category_id, count in product_count_result.all()
    }
    children: dict[uuid.UUID | None, list[Category]] = {}
    for category in all_categories:
        children.setdefault(category.parent_id, []).append(category)

    def total_for(category: Category, seen: set[uuid.UUID]) -> int:
        if category.id in seen:
            return direct_counts.get(category.id, 0)
        next_seen = {*seen, category.id}
        return direct_counts.get(category.id, 0) + sum(
            total_for(child, next_seen)
            for child in children.get(category.id, [])
        )

    rows = []
    for category in all_categories[: max(int(limit), 1)]:
        product_count = total_for(category, set())
        row = serialize_record(category)
        row["product_count"] = product_count
        if _safe_public_display_text(row.get("name")) or _safe_public_display_text(row.get("name_en")):
            rows.append(row)
    return rows


@router.get("/api/catalog/categories")
async def catalog_categories(limit: int = Query(500, ge=1, le=5000), session: AsyncSession = Depends(get_session)):
    return {"data": await categories(limit=limit, session=session)}


@router.get("/api/catalog/admin/categories")
async def catalog_admin_categories(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Category).where(Category.deleted_at.is_(None)).order_by(Category.sort_order, Category.name))
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.get("/brands")
async def brands(limit: int = Query(100, ge=1, le=5000), session: AsyncSession = Depends(get_session)):
    key = cache_key("catalog-brands", limit=limit)
    return await public_read_cache.get_or_set(key, lambda: _brands_uncached(limit=limit, session=session))


async def _brands_uncached(limit: int, session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(Brand)
        .where(Brand.deleted_at.is_(None), Brand.is_active.is_(True))
        .order_by(Brand.name)
        .limit(limit)
    )
    return [
        row
        for row in (serialize_record(item) for item in result.scalars())
        if _safe_public_display_text(row.get("name")) or _safe_public_display_text(row.get("name_en"))
    ]

@router.get("/api/catalog/brands")
async def catalog_brands(limit: int = Query(500, ge=1, le=5000), session: AsyncSession = Depends(get_session)):
    return {"data": await brands(limit=limit, session=session)}


@router.get("/api/catalog/currencies")
async def catalog_currencies(limit: int = Query(100, ge=1, le=5000), session: AsyncSession = Depends(get_session)):
    key = cache_key("catalog-currencies", limit=limit)
    return await public_read_cache.get_or_set(key, lambda: _catalog_currencies_uncached(limit=limit, session=session))


async def _catalog_currencies_uncached(limit: int, session: AsyncSession) -> dict[str, Any]:
    model = MODEL_BY_TABLE["currencies"]
    statement = select(model)
    if "deleted_at" in model.__table__.c:
        statement = statement.where(model.__table__.c.deleted_at.is_(None))
    if "is_active" in model.__table__.c:
        statement = statement.where(model.__table__.c.is_active.is_(True))
    if "status" in model.__table__.c:
        statement = statement.where(model.__table__.c.status.notin_(["disabled", "inactive", "deleted"]))
    if "sort_order" in model.__table__.c:
        statement = statement.order_by(model.__table__.c.sort_order)
    elif "code" in model.__table__.c:
        statement = statement.order_by(model.__table__.c.code)
    result = await session.execute(statement.limit(limit))
    rows = [serialize_record(row) for row in result.scalars()]
    if rows:
        return {"data": rows}
    # Keep international shopping usable on a fresh deployment before the
    # administrator has populated the currency table. USD intentionally has
    # no invented rate; the customer may submit the foreign-currency estimate
    # and the final conversion is confirmed before purchase.
    return {
        "data": [
            {
                "id": "default-YER",
                "code": "YER",
                "name": "الريال اليمني",
                "name_en": "Yemeni Rial",
                "symbol": "ر.ي",
                "exchange_rate": 1,
                "is_default": True,
                "is_active": True,
                "sort_order": 0,
            },
            {
                "id": "default-USD",
                "code": "USD",
                "name": "الدولار الأمريكي",
                "name_en": "US Dollar",
                "symbol": "$",
                "exchange_rate": None,
                "is_default": False,
                "is_active": True,
                "sort_order": 1,
            },
        ][: max(int(limit), 1)],
    }


@router.get("/api/catalog/admin/brands")
async def catalog_admin_brands(staff: User = Depends(require_staff), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Brand).where(Brand.deleted_at.is_(None)).order_by(Brand.name))
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.get("/products")
async def products(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=5000),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    featuredOnly: bool = False,
    featured: bool | None = None,
    newOnly: bool = False,
    new_only: bool | None = None,
    is_new: bool | None = None,
    onSale: bool = False,
    search: str | None = None,
    q: str | None = None,
    categoryId: str | None = None,
    categoryIds: str | None = None,
    categorySlug: str | None = None,
    categoryName: str | None = None,
    brand: str | None = None,
    brandId: str | None = None,
    brandSlug: str | None = None,
    partnerId: str | None = None,
    supplierId: str | None = None,
    mainStoreOnly: bool | None = None,
    minPrice: Decimal | None = None,
    maxPrice: Decimal | None = None,
    sort: str = "newest",
    session: AsyncSession = Depends(get_session),
):
    response = await _catalog_products_page(
        session,
        page=page if not offset else (offset // (limit or 24)) + 1,
        page_size=page_size or limit or 24,
        offset=offset,
        sort=sort,
        include_total=False,
        featured=bool(featured or featuredOnly),
        on_sale=onSale,
        new_only=bool(new_only or newOnly or is_new),
        search=q or search,
        category_id=categoryId,
        category_ids=categoryIds,
        category_slug=categorySlug,
        category_name=categoryName,
        brand=brand,
        brand_id=brandId,
        brand_slug=brandSlug,
        supplier_id=supplierId,
        partner_id=partnerId or supplierId,
        main_store_only=bool(mainStoreOnly),
        min_price=minPrice,
        max_price=maxPrice,
    )
    return response["items"]


@router.get("/api/catalog/products")
async def catalog_products(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=5000),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int | None = Query(None, ge=0),
    q: str | None = None,
    search: str | None = None,
    categoryId: str | None = None,
    categoryIds: str | None = None,
    categorySlug: str | None = None,
    categoryName: str | None = None,
    brand: str | None = None,
    brandId: str | None = None,
    brandSlug: str | None = None,
    supplierId: str | None = None,
    partnerId: str | None = None,
    mainStoreOnly: bool = False,
    featured: bool = False,
    featuredOnly: bool = False,
    newOnly: bool = False,
    new_only: bool | None = None,
    is_new: bool | None = None,
    onSale: bool = False,
    includeTotal: bool = True,
    minPrice: Decimal | None = None,
    maxPrice: Decimal | None = None,
    sort: str = "newest",
    session: AsyncSession = Depends(get_session),
):
    return await _catalog_products_page(
        session,
        page=page,
        page_size=page_size or limit or 20,
        offset=offset,
        sort=sort,
        include_total=includeTotal,
        featured=featured or featuredOnly,
        on_sale=onSale,
        new_only=bool(new_only or newOnly or is_new),
        search=q or search,
        category_id=categoryId,
        category_ids=categoryIds,
        category_slug=categorySlug,
        category_name=categoryName,
        brand=brand,
        brand_id=brandId,
        brand_slug=brandSlug,
        supplier_id=supplierId,
        partner_id=partnerId,
        main_store_only=mainStoreOnly,
        min_price=minPrice,
        max_price=maxPrice,
    )


@router.get("/api/catalog/offers")
@router.get("/offers")
async def offers(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=5000),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    q: str | None = None,
    search: str | None = None,
    categoryId: str | None = None,
    categoryIds: str | None = None,
    categorySlug: str | None = None,
    categoryName: str | None = None,
    brand: str | None = None,
    brandId: str | None = None,
    brandSlug: str | None = None,
    supplierId: str | None = None,
    partnerId: str | None = None,
    mainStoreOnly: bool = False,
    minPrice: Decimal | None = None,
    maxPrice: Decimal | None = None,
    sort: str = "discount",
    session: AsyncSession = Depends(get_session),
):
    return await _catalog_products_page(
        session,
        page=page,
        page_size=page_size or limit or 20,
        offset=offset,
        sort=sort,
        on_sale=True,
        search=q or search,
        category_id=categoryId,
        category_ids=categoryIds,
        category_slug=categorySlug,
        category_name=categoryName,
        brand=brand,
        brand_id=brandId,
        brand_slug=brandSlug,
        supplier_id=supplierId,
        partner_id=partnerId,
        main_store_only=mainStoreOnly,
        min_price=minPrice,
        max_price=maxPrice,
    )


@router.get("/products/{product_id}")
async def product_detail(product_id: str, session: AsyncSession = Depends(get_session)):
    identifier = unquote(product_id).strip()
    if not identifier:
        raise HTTPException(status_code=404, detail="product_not_found")
    return await public_read_cache.get_or_set(
        cache_key("catalog-product-detail", identifier=identifier),
        lambda: _product_detail_uncached(identifier, session),
    )


async def _product_detail_uncached(identifier: str, session: AsyncSession) -> dict[str, Any]:
    lookup_clauses = []
    try:
        lookup_clauses.append(Product.id == uuid.UUID(identifier))
    except ValueError:
        compact_uuid = decode_compact_uuid(identifier)
        if compact_uuid is not None:
            lookup_clauses.append(Product.id == compact_uuid)
    lookup_clauses.append(Product.short_code == identifier)
    lookup_clauses.append(Product.sku == identifier)
    product = (
        await session.execute(
            select(Product).where(or_(*lookup_clauses), *public_product_clauses(Product)).limit(1)
        )
    ).scalar_one_or_none()
    validate_public_product_or_404(product)
    rows = await build_public_product_rows(session, [product], include_variants=True)
    if not rows:
        raise HTTPException(status_code=404, detail="product_not_found")
    return rows[0]


@router.get("/api/catalog/products/{identifier}")
async def catalog_product_detail(identifier: str, session: AsyncSession = Depends(get_session)):
    return {"data": await product_detail(identifier, session)}


@router.get("/partner-storefronts")
async def partner_storefronts(limit: int = Query(80, ge=1, le=5000), session: AsyncSession = Depends(get_session)):
    return await public_read_cache.get_or_set(
        cache_key("catalog-stores", limit=limit),
        lambda: _partner_storefronts_uncached(limit=limit, session=session),
    )


async def _partner_storefronts_uncached(limit: int, session: AsyncSession):
    model = MODEL_BY_TABLE["partner_storefronts"]
    local_model = MODEL_BY_TABLE["local_merchants"]
    result = await session.execute(
        select(model)
        .where(model.deleted_at.is_(None), model.is_active.is_(True))
        .order_by(model.name)
        .limit(limit)
    )
    storefront_rows = list(result.scalars())
    local_result = await session.execute(
        select(local_model)
        .where(local_model.deleted_at.is_(None), local_model.is_active.is_(True))
        .order_by(local_model.name)
        .limit(limit)
    )
    local_merchant_rows = list(local_result.scalars())
    storefront_partner_ids = {
        value
        for item in storefront_rows
        for value in (getattr(item, "partner_id", None), getattr(item, "user_id", None))
        if value
    }
    local_merchant_ids = {getattr(item, "id", None) for item in local_merchant_rows if getattr(item, "id", None)}
    product_owner_clause = [Product.partner_id.is_(None)]
    if storefront_partner_ids:
        product_owner_clause.append(Product.partner_id.in_(storefront_partner_ids))
    if local_merchant_ids:
        product_owner_clause.append(Product.supplier_id.in_(local_merchant_ids))
    count_result = await session.execute(
        select(Product.supplier_id, Product.partner_id, func.count(Product.id))
        .where(*public_product_clauses(Product))
        .where(or_(*product_owner_clause))
        .group_by(Product.supplier_id, Product.partner_id)
    )
    partner_product_counts: dict[Any, int] = {}
    local_product_counts: dict[Any, int] = {}
    main_store_count = 0
    for supplier_id, partner_id, count in count_result.all():
        safe_count = int(count or 0)
        if partner_id is None and supplier_id not in local_merchant_ids:
            main_store_count += safe_count
        if partner_id is not None:
            partner_product_counts[partner_id] = partner_product_counts.get(partner_id, 0) + safe_count
        if supplier_id in local_merchant_ids:
            local_product_counts[supplier_id] = local_product_counts.get(supplier_id, 0) + safe_count
    rows = []
    if main_store_count > 0:
        rows.append(public_main_storefront_response(products_count=main_store_count))
    store_rows = [(item, False) for item in storefront_rows] + [(item, True) for item in local_merchant_rows]
    store_rows.sort(key=lambda entry: str(getattr(entry[0], "name", None) or "").casefold())
    for item, is_local_merchant in store_rows:
        if _safe_public_display_text(getattr(item, "name", None)):
            partner_values = {
                value
                for value in (getattr(item, "partner_id", None), getattr(item, "user_id", None))
                if value
            }
            if is_local_merchant:
                count = int(local_product_counts.get(item.id, 0) or 0)
            else:
                count = sum(int(partner_product_counts.get(value, 0) or 0) for value in partner_values)
            # A local merchant is an explicit admin-managed storefront and
            # should be discoverable before its first product is published.
            if not is_local_merchant and count <= 0:
                continue
            rows.append(
                public_storefront_response(
                    item,
                    products_count=count,
                    public_id=item.id if is_local_merchant else None,
                    store_type="local" if is_local_merchant else "partner",
                )
            )
    # A partner id without a real storefront record is not a customer-facing
    # store. Do not invent a UUID-based name for it in the public catalog.
    return rows[:limit]


MAX_CATALOG_IMAGE_BYTES = 12 * 1024 * 1024


def _catalog_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _canonicalize_catalog_image(data: bytes) -> tuple[bytes, str] | None:
    media_type = _catalog_image_mime(data)
    if media_type is None:
        return None
    # A legacy object can be named .webp while carrying JPEG bytes and omit
    # only the terminal EOI marker. Restore that marker before decoding.
    if media_type == "image/jpeg" and not data.endswith(b"\xff\xd9"):
        data = data + b"\xff\xd9"
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            image.load()
            # Android devices are not consistent when an object is named
            # .webp but the edge/CDN metadata is incomplete. Return one
            # decoder-safe representation from the proxy so the URL suffix,
            # Content-Type, and bytes always agree.
            if media_type == "image/webp":
                converted = image.convert("RGB")
                output = BytesIO()
                converted.save(output, format="JPEG", quality=92, optimize=True)
                return output.getvalue(), "image/jpeg"
    except Exception:
        return None
    return data, media_type


@router.get("/catalog/image-proxy/{image_path:path}")
async def catalog_image_proxy(image_path: str):
    """Serve legacy CDN images with bytes and Content-Type aligned.

    This route is deliberately allow-listed to one historical image hostname;
    it is not a general URL fetcher and therefore cannot become an SSRF proxy.
    """
    relative = str(image_path or "").replace("\\", "/").lstrip("/")
    if not relative or ".." in Path(relative).parts:
        raise HTTPException(status_code=404, detail="image_not_found")
    # Legacy banner records may point at the public site's hashed /assets
    # files, while older catalog records point at the historical image CDN.
    # Keep both upstreams explicit; this remains an allow-listed image proxy,
    # never a general URL fetcher.
    upstream_host = "luxuryshoppings.com" if relative.lower().startswith("assets/") else "images.luxuryshoppings.com"
    source = f"https://{upstream_host}/{relative}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            headers={"Accept": "image/*", "User-Agent": "LuxuryShoppingImageProxy/1.0"},
        ) as client:
            async with client.stream("GET", source) as upstream:
                if upstream.status_code != 200:
                    raise HTTPException(status_code=404, detail="image_not_found")
                declared_size = int(upstream.headers.get("content-length") or 0)
                if declared_size > MAX_CATALOG_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="image_too_large")
                chunks: list[bytes] = []
                total = 0
                async for chunk in upstream.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_CATALOG_IMAGE_BYTES:
                        raise HTTPException(status_code=413, detail="image_too_large")
                    chunks.append(chunk)
                if declared_size and total != declared_size:
                    raise HTTPException(status_code=502, detail="image_incomplete")
    except HTTPException:
        raise
    except (httpx.HTTPError, OSError, ValueError) as error:
        raise HTTPException(status_code=502, detail="image_upstream_unavailable") from error
    canonical = _canonicalize_catalog_image(b"".join(chunks))
    if canonical is None:
        raise HTTPException(status_code=502, detail="image_invalid")
    data, media_type = canonical
    return Response(
        data,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400, s-maxage=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/stores")
@router.get("/partners")
async def public_stores(limit: int = Query(500, ge=1, le=5000), session: AsyncSession = Depends(get_session)):
    return await partner_storefronts(limit=limit, session=session)


@router.get("/api/catalog/stores")
async def catalog_stores(limit: int = Query(500, ge=1, le=5000), session: AsyncSession = Depends(get_session)):
    return {"data": await partner_storefronts(limit=limit, session=session)}


@router.get("/cart")
async def get_cart(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(UserCart, Product)
        .join(Product, Product.id == UserCart.product_id)
        .where(UserCart.user_id == user.id)
        .order_by(UserCart.created_at.desc())
    )
    rows = []
    for item, product in result.all():
        row = serialize_record(item)
        row["product"] = await _product_payload(session, product)
        variant = None
        if item.variant_id:
            variant = await session.get(ProductVariant, item.variant_id)
        try:
            await eligible_line(
                session,
                product=product,
                variant=variant,
                variant_id=item.variant_id,
                quantity=parse_strict_quantity(item.quantity),
            )
            row["is_available_for_checkout"] = True
            row["availability_status"] = "available"
        except HTTPException as exc:
            row["is_available_for_checkout"] = False
            row["availability_status"] = "unavailable"
            row["availability_error"] = exc.detail
        rows.append(row)
    return rows


@router.get("/api/cart")
async def api_get_cart(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return {"data": await get_cart(user, session)}


@router.put("/api/cart")
async def api_sync_cart(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    items = body.get("items") if isinstance(body, dict) else []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items_required")
    validated: list[tuple[uuid.UUID, uuid.UUID | None, int]] = []
    for item in items:
        product_id = _uuid(item.get("productId") or item.get("product_id"), "productId")
        variant_id = _uuid(item.get("variantId") or item.get("variant_id"), "variantId") if item.get("variantId") or item.get("variant_id") else None
        quantity = parse_strict_quantity(item.get("quantity") if "quantity" in item else 1)
        product = (
            await session.execute(select(Product).where(Product.id == product_id).with_for_update())
        ).scalar_one_or_none()
        variant = None
        if variant_id:
            variant = (
                await session.execute(select(ProductVariant).where(ProductVariant.id == variant_id).with_for_update())
            ).scalar_one_or_none()
        await eligible_line(session, product=product, variant=variant, variant_id=variant_id, quantity=quantity)
        validated.append((product_id, variant_id, quantity))
    await session.execute(delete(UserCart).where(UserCart.user_id == user.id))
    rows = []
    for product_id, variant_id, quantity in validated:
        row = UserCart(user_id=user.id, product_id=product_id, variant_id=variant_id, quantity=quantity)
        session.add(row)
        rows.append(row)
    await session.commit()
    return {"data": [serialize_record(row) for row in rows]}


@router.post("/api/catalog/cart/hydrate")
async def api_hydrate_cart(request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.json()
    items = body.get("items") if isinstance(body, dict) else []
    product_ids = []
    variant_ids = []
    for item in items if isinstance(items, list) else []:
        try:
            product_ids.append(uuid.UUID(str(item.get("productId") or item.get("product_id"))))
        except Exception:
            pass
        if item.get("variantId") or item.get("variant_id"):
            try:
                variant_ids.append(uuid.UUID(str(item.get("variantId") or item.get("variant_id"))))
            except Exception:
                pass
    products = []
    variants = []
    if product_ids:
        public_products = list(
            (
                await session.execute(
                    select(Product).where(Product.id.in_(product_ids), *public_product_clauses(Product))
                )
            ).scalars()
        )
        products = await build_public_product_rows(session, public_products)
    if variant_ids:
        variants = [serialize_record(row) for row in (await session.execute(select(ProductVariant).where(ProductVariant.id.in_(variant_ids), ProductVariant.deleted_at.is_(None)))).scalars()]
    return {"products": products, "variants": variants}


@router.delete("/api/cart")
async def api_clear_cart(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await clear_cart(user, session)


@router.post("/cart")
async def add_cart(
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    product_id = _uuid(body.get("productId"), "productId")
    variant_id = _uuid(body.get("variantId"), "variantId") if body.get("variantId") else None
    quantity = parse_strict_quantity(body.get("quantity") if "quantity" in body else 1)
    product = (
        await session.execute(select(Product).where(Product.id == product_id).with_for_update())
    ).scalar_one_or_none()
    variant = None
    if variant_id:
        variant = (
            await session.execute(select(ProductVariant).where(ProductVariant.id == variant_id).with_for_update())
        ).scalar_one_or_none()
    await _advisory_xact_lock(session, f"cart:{user.id}:{product_id}:{variant_id or 'no-variant'}")
    result = await session.execute(
        select(UserCart).where(
            UserCart.user_id == user.id,
            UserCart.product_id == product_id,
            UserCart.variant_id.is_(variant_id) if variant_id is None else UserCart.variant_id == variant_id,
        ).with_for_update()
    )
    item = result.scalar_one_or_none()
    target_quantity = (item.quantity if item else 0) + quantity
    await eligible_line(session, product=product, variant=variant, variant_id=variant_id, quantity=target_quantity)
    if item:
        item.quantity = target_quantity
    else:
        item = UserCart(user_id=user.id, product_id=product_id, variant_id=variant_id, quantity=quantity)
        session.add(item)
        response.status_code = 201
    await session.commit()
    row = serialize_record(item)
    row["product"] = await _product_payload(session, product)
    return row


@router.patch("/cart/{cart_id}")
async def update_cart(
    cart_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    quantity = parse_strict_quantity(body.get("quantity"))
    result = await session.execute(
        select(UserCart).where(UserCart.id == cart_id, UserCart.user_id == user.id).with_for_update()
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="cart_item_not_found")
    product = (
        await session.execute(select(Product).where(Product.id == item.product_id).with_for_update())
    ).scalar_one_or_none()
    variant = None
    if item.variant_id:
        variant = (
            await session.execute(select(ProductVariant).where(ProductVariant.id == item.variant_id).with_for_update())
        ).scalar_one_or_none()
    await eligible_line(session, product=product, variant=variant, variant_id=item.variant_id, quantity=quantity)
    item.quantity = quantity
    await session.commit()
    return serialize_record(item)


@router.delete("/cart/{cart_id}")
async def delete_cart(cart_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(delete(UserCart).where(UserCart.id == cart_id, UserCart.user_id == user.id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="cart_item_not_found")
    await session.commit()
    return {"ok": True}


@router.delete("/cart")
async def clear_cart(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await session.execute(delete(UserCart).where(UserCart.user_id == user.id))
    await session.commit()
    return {"ok": True}


@router.get("/wishlist")
async def get_wishlist(limit: int = Query(500, ge=1, le=2000), user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Wishlist).where(Wishlist.user_id == user.id).limit(limit))
    return [serialize_record(row) for row in result.scalars()]


@router.post("/wishlist")
async def add_wishlist(request: Request, response: Response, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    body = await request.json()
    product_id = _uuid(body.get("productId"), "productId")
    product = await session.get(Product, product_id)
    await eligible_line(session, product=product, quantity=1)
    existing = await session.execute(select(Wishlist).where(Wishlist.user_id == user.id, Wishlist.product_id == product_id))
    item = existing.scalar_one_or_none()
    if item is None:
        item = Wishlist(user_id=user.id, product_id=product_id)
        session.add(item)
        response.status_code = 201
        await session.commit()
    return serialize_record(item)


@router.delete("/wishlist/{product_id}")
async def delete_wishlist(product_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await session.execute(delete(Wishlist).where(Wishlist.user_id == user.id, Wishlist.product_id == product_id))
    await session.commit()
    return Response(status_code=204)


async def _visible_orders(
    session: AsyncSession,
    user: User,
    roles: set[str],
    scope: str | None,
    limit: int,
    partner_id: uuid.UUID | None = None,
):
    statement = select(Order).where(Order.deleted_at.is_(None))
    if scope == "admin" and roles.intersection({"admin", "manager", "finance", "logistics", "staff", "employee"}):
        if partner_id is not None:
            order_ids = select(OrderItem.order_id).where(OrderItem.partner_id == partner_id)
            statement = statement.where(Order.id.in_(order_ids))
    elif scope == "partner" and "partner" in roles:
        order_ids = select(OrderItem.order_id).where(OrderItem.partner_id == user.id)
        statement = statement.where(Order.id.in_(order_ids))
    else:
        statement = statement.where(Order.user_id == user.id)
    result = await session.execute(statement.order_by(Order.created_at.desc()).limit(limit))
    return list(result.scalars())


async def _serialize_orders_with_financials(session: AsyncSession, orders: list[Order]) -> list[dict[str, Any]]:
    """Return order rows with one consistent paid/remaining summary.

    The checkout model stores verified receipts in ``payments`` while the
    administration panel also records manual payment entries in
    ``order_payments``. Both are valid ledger sources; pending entries are
    deliberately excluded from the paid amount.
    """
    payloads = [_serialize_order(order) for order in orders]
    if not orders:
        return payloads

    order_ids = [order.id for order in orders]
    recognized_statuses = ("confirmed", "approved", "paid", "completed")
    paid_by_order: dict[uuid.UUID, Decimal] = {order.id: Decimal("0.00") for order in orders}
    for table_name in ("order_payments", "payments"):
        payment_model = MODEL_BY_TABLE[table_name]
        result = await session.execute(
            select(payment_model.order_id, func.coalesce(func.sum(payment_model.amount), 0))
            .where(
                payment_model.order_id.in_(order_ids),
                payment_model.deleted_at.is_(None),
                func.lower(payment_model.status).in_(recognized_statuses),
            )
            .group_by(payment_model.order_id)
        )
        for order_id, amount in result.all():
            paid_by_order[order_id] = money(paid_by_order.get(order_id, Decimal("0.00")) + _money(amount))

    for order, payload in zip(orders, payloads):
        # Keep any older derived value when it is larger than the new ledger
        # total; this makes the rollout safe for legacy orders.
        paid = max(paid_by_order.get(order.id, Decimal("0.00")), _money(payload.get("paid_amount")))
        total = _money(payload.get("total"))
        payload["paid_amount"] = format(money(paid), "f")
        payload["remaining_balance"] = format(money(max(total - paid, Decimal("0.00"))), "f")
        if paid > 0 and str(payload.get("payment_status") or "").lower() not in {"refunded", "partial_refund", "partially_refunded"}:
            payload["payment_status"] = "paid" if total > 0 and paid >= total else "partial"
        payload.setdefault("shipping_cost", payload.get("shipping_total", "0"))
    return payloads


@router.get("/orders")
@router.get("/api/orders")
async def orders(
    request: Request,
    limit: int = Query(50, ge=1, le=1000),
    scope: str | None = None,
    partnerId: str | None = None,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if scope == "partner" and "partner" in roles and not roles.intersection({"admin", "manager", "finance", "logistics", "staff", "employee"}):
        rows = await merchant_order_list(session, partner_id=user.id, limit=limit)
        return {"data": rows} if request.url.path.startswith("/api/") else rows
    partner_uuid = _uuid(partnerId, "partnerId") if partnerId else None
    rows = await _serialize_orders_with_financials(session, await _visible_orders(session, user, roles, scope, limit, partner_id=partner_uuid))
    return {"data": rows} if request.url.path.startswith("/api/") else rows


@router.get("/api/partner/orders")
async def api_partner_orders(
    limit: int = Query(50, ge=1, le=1000),
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if "partner" not in roles:
        raise HTTPException(status_code=403, detail="partner_required")
    return {"data": await merchant_order_list(session, partner_id=user.id, limit=limit)}


def _partner_request_payload(row: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    notes = getattr(row, "notes", None)
    if isinstance(getattr(row, "extra_data", None), dict):
        payload.update(row.extra_data or {})
    if isinstance(notes, str) and notes.strip():
        try:
            decoded = json.loads(notes)
            if isinstance(decoded, dict):
                payload.update(decoded)
            else:
                payload.setdefault("description", notes)
        except json.JSONDecodeError:
            payload.setdefault("description", notes)
    return {
        **serialize_record(row),
        "request_type": payload.get("request_type") or "custom",
        "title": payload.get("title") or "طلب تاجر",
        "description": payload.get("description") or "",
        "estimated_value": payload.get("estimated_value"),
    }


@router.get("/api/partner/requests")
async def api_partner_requests(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if "partner" not in roles:
        raise HTTPException(status_code=403, detail="partner_required")
    model = MODEL_BY_TABLE["partner_order_requests"]
    result = await session.execute(
        select(model)
        .where(model.partner_id == user.id, model.deleted_at.is_(None))
        .order_by(model.created_at.desc())
        .limit(limit)
    )
    return {"data": [_partner_request_payload(row) for row in result.scalars()]}


@router.post("/api/partner/requests")
async def api_create_partner_request(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if "partner" not in roles:
        raise HTTPException(status_code=403, detail="partner_required")
    body = await request.json()
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title_required")
    request_type = str(body.get("request_type") or "custom").strip() or "custom"
    description = str(body.get("description") or "").strip()
    estimated_value = body.get("estimated_value")
    payload = {
        "request_type": request_type,
        "title": title,
        "description": description,
        "estimated_value": estimated_value,
    }
    model = MODEL_BY_TABLE["partner_order_requests"]
    async with session.begin_nested():
        row = model(
            partner_id=user.id,
            status="pending",
            notes=json.dumps(payload, ensure_ascii=False),
            extra_data=payload,
        )
        session.add(row)
        await session.flush()
    await session.commit()
    return {"data": _partner_request_payload(row)}


async def _create_notification(session: AsyncSession, table_name: str, **values: Any) -> None:
    if table_name == "notifications" and values.get("user_id"):
        order_id = values.get("order_id")
        if order_id is not None and not isinstance(order_id, uuid.UUID):
            order_id = _uuid(order_id, "order_id")
        payload = values.get("payload") if isinstance(values.get("payload"), dict) else {}
        if order_id is not None:
            payload = {**payload, "order_id": str(order_id)}
        await NotificationService(session).create_notification(
            NotificationPayload(
                user_id=values["user_id"],
                title=str(values.get("title") or "رفاهية التسوق"),
                body=str(values.get("body") or values.get("message") or ""),
                notification_type=str(values.get("type") or values.get("notification_type") or "message"),
                category=str(values.get("category") or "system"),
                priority=str(values.get("priority") or "normal"),
                image_url=values.get("image_url"),
                action_type=values.get("action_type"),
                action_url=values.get("url"),
                entity_type=values.get("entity_type") or ("order" if order_id else None),
                entity_id=values.get("entity_id") or (str(order_id) if order_id else None),
                order_id=order_id,
                payload=payload,
                created_by=values.get("created_by"),
                source=str(values.get("source") or "commerce"),
                deduplication_key=values.get("deduplication_key"),
                expires_at=values.get("expires_at"),
            )
        )
        return
    model = MODEL_BY_TABLE[table_name]
    session.add(model(**values))


async def _record_financial_side_effects(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    order_id: uuid.UUID,
    financials: Any,
) -> None:
    if financials.coupon_id and financials.coupon_discount > 0:
        usage_model = MODEL_BY_TABLE["coupon_usage"]
        session.add(usage_model(
            user_id=user_id,
            order_id=order_id,
            amount=financials.coupon_discount,
            extra_data={"coupon_id": financials.coupon_id, "amount": str(financials.coupon_discount)},
        ))
    if financials.loyalty_discount > 0:
        loyalty_model = MODEL_BY_TABLE["user_loyalty"]
        loyalty = (
            await session.execute(
                select(loyalty_model).where(loyalty_model.user_id == user_id).with_for_update().limit(1)
            )
        ).scalar_one_or_none()
        if loyalty is None or money(loyalty.balance or 0) < financials.loyalty_discount:
            raise HTTPException(status_code=409, detail="insufficient_loyalty_points")
        loyalty.balance = money(loyalty.balance or 0) - financials.loyalty_discount
        tx_model = MODEL_BY_TABLE["points_transactions"]
        session.add(tx_model(
            user_id=user_id,
            order_id=order_id,
            type="redeem",
            amount=financials.loyalty_discount,
            description="Redeemed loyalty points during checkout",
            extra_data={"policy": "1_point_equals_1_YER", "order_id": str(order_id)},
        ))


async def _validated_cart_lines(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> tuple[list[tuple[UserCart, Product, ProductVariant | None, Decimal]], Decimal, Decimal]:
    cart_result = await session.execute(
        select(UserCart).where(UserCart.user_id == user_id).with_for_update()
    )
    cart_items = list(cart_result.scalars())
    if not cart_items:
        raise HTTPException(status_code=400, detail="cart_empty")
    subtotal = Decimal("0.00")
    product_discount = Decimal("0.00")
    lines: list[tuple[UserCart, Product, ProductVariant | None, Decimal]] = []
    for item in cart_items:
        quantity = parse_strict_quantity(item.quantity)
        product = (
            await session.execute(select(Product).where(Product.id == item.product_id).with_for_update())
        ).scalar_one_or_none()
        variant = None
        if item.variant_id:
            variant = (
                await session.execute(select(ProductVariant).where(ProductVariant.id == item.variant_id).with_for_update())
            ).scalar_one_or_none()
        line = await eligible_line(
            session,
            product=product,
            variant=variant,
            variant_id=item.variant_id,
            quantity=quantity,
        )
        original_unit = _line_original_unit(line.product, line.variant, line.unit_price)
        subtotal += line_total(original_unit, quantity)
        product_discount += line_total(original_unit - line.unit_price, quantity)
        lines.append((item, line.product, line.variant, line.unit_price))
    return lines, money(subtotal), money(product_discount)


@router.post("/checkout/preview")
@router.post("/api/checkout/preview")
async def checkout_preview(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid_checkout_request")
    validate_payment_method = await validate_payment_method_for_checkout(
        session,
        body.get("paymentMethod") or body.get("payment_method"),
    )
    shipping_address = validate_customer_checkout_address(
        body.get("shippingAddress") or body.get("shipping_address")
    )
    body = {**body, "shippingAddress": shipping_address}
    lines, subtotal, product_discount = await _validated_cart_lines(session, user.id)
    financials = await calculate_checkout_financials(
        session,
        user_id=user.id,
        subtotal=subtotal,
        product_discount=product_discount,
        body=body,
    )
    return {
        "items": [
            {
                "cart_item_id": str(item.id),
                "product_id": str(product.id),
                "variant_id": str(variant.id) if variant else None,
                "quantity": item.quantity,
                "unit_price": str(money(unit)),
                "line_total": str(line_total(unit, item.quantity)),
            }
            for item, product, variant, unit in lines
        ],
        "unavailable_items": [],
        "subtotal": str(financials.subtotal),
        "product_discount": str(financials.product_discount),
        "coupon_discount": str(financials.coupon_discount),
        "loyalty_discount": str(financials.loyalty_discount),
        "shipping_cost": str(financials.shipping_total),
        "total": str(financials.total),
        "currency": "YER",
        "coupon_status": financials.breakdown.get("coupon"),
        "points_to_redeem": body.get("loyaltyPointsToRedeem") or body.get("loyaltyPoints") or 0,
        "validation_warnings": [],
        "pricing_version": financials.breakdown["policy"],
        "breakdown": financials.breakdown,
    }


@router.post("/orders/checkout")
async def checkout(
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid_checkout_request")
    payment_method = await validate_payment_method_for_checkout(
        session,
        body.get("paymentMethod") or body.get("payment_method"),
    )
    shipping_address = validate_customer_checkout_address(
        body.get("shippingAddress") or body.get("shipping_address")
    )
    body = {**body, "shippingAddress": shipping_address}
    key = _normalize_idempotency_key(idempotency_key or body.get("idempotencyKey"))
    request_hash = _request_hash(body)
    if key:
        await _advisory_xact_lock(session, f"idempotency:/orders/checkout:{key}")
        existing = await session.execute(select(Order).where(Order.idempotency_key == str(key)))
        order = existing.scalar_one_or_none()
        if order:
            return _idempotency_replay_response(
                order,
                actor_id=user.id,
                endpoint="/orders/checkout",
                request_hash=request_hash,
            )
    async with session.begin_nested():
        lines, subtotal, product_discount = await _validated_cart_lines(session, user.id)
        financials = await calculate_checkout_financials(
            session,
            user_id=user.id,
            subtotal=subtotal,
            product_discount=product_discount,
            body=body,
        )
        order = Order(
            order_number=f"ORD-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            user_id=user.id,
            status="pending",
            subtotal=financials.subtotal,
            shipping_total=financials.shipping_total,
            discount_total=financials.discount_total,
            total=financials.total,
            currency_code="YER",
            payment_method=payment_method,
            payment_status="pending",
            shipping_address=shipping_address,
            notes=body.get("notes"),
            idempotency_key=str(key) if key else None,
            extra_data={
                "financial_breakdown": financials.breakdown,
                "coupon_id": financials.coupon_id,
                "shipping_source": financials.shipping_source,
                **({
                    "idempotency_actor_id": str(user.id),
                    "idempotency_endpoint": "/orders/checkout",
                    "idempotency_request_hash": request_hash,
                } if key else {}),
            },
        )
        session.add(order)
        await session.flush()
        await _record_financial_side_effects(
            session,
            user_id=user.id,
            order_id=order.id,
            financials=financials,
        )
        for item, product, variant, calculated_unit_price in lines:
            session.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                variant_id=variant.id if variant else None,
                product_name=product.name,
                product_image=product.image_url,
                quantity=item.quantity,
                unit_price=calculated_unit_price,
                total_price=line_total(calculated_unit_price, item.quantity),
                partner_id=product.partner_id,
                extra_data={"pricing_snapshot": {"unit_price": str(money(calculated_unit_price)), "quantity": item.quantity}},
            ))
            if product.track_inventory:
                if variant:
                    variant.stock_quantity -= item.quantity
                else:
                    product.stock_quantity -= item.quantity
        history_model = MODEL_BY_TABLE["order_status_history"]
        session.add(history_model(order_id=order.id, status="pending", notes="Order created", extra_data={"new_status": "pending"}))
        payment_model = MODEL_BY_TABLE["order_payments"]
        session.add(payment_model(order_id=order.id, status="pending", type=payment_method, amount=financials.total))
        await _create_notification(
            session, "notifications", user_id=user.id, recipient_id=user.id,
            order_id=order.id, title="تم استلام طلبك", body=f"تم إنشاء الطلب {order.order_number}",
            message=f"تم إنشاء الطلب {order.order_number}", type="order_created", status="new", is_read=False,
        )
        await _create_notification(
            session, "admin_notifications", title="طلب جديد",
            body=f"وصل طلب جديد {order.order_number}", message=f"وصل طلب جديد {order.order_number}",
            type="new_order", status="new", is_read=False, extra_data={"order_id": str(order.id)},
        )
        await session.execute(delete(UserCart).where(UserCart.user_id == user.id))
    await session.commit()
    response.status_code = 201
    return _serialize_order(order, idempotency_replayed=False if key else None)


@router.post("/admin/manual-order")
async def create_manual_order(
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    admin: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid_manual_order_request")
    payment_method = await validate_payment_method_for_checkout(
        session,
        body.get("paymentMethod") or body.get("payment_method"),
    )
    key = _normalize_idempotency_key(idempotency_key or body.get("idempotencyKey"))
    request_hash = _request_hash(body)
    if key:
        await _advisory_xact_lock(session, f"idempotency:/admin/manual-order:{key}")
        existing = await session.execute(
            select(Order).where(Order.idempotency_key == str(key))
        )
        previous = existing.scalar_one_or_none()
        if previous is not None:
            return _idempotency_replay_response(
                previous,
                actor_id=admin.id,
                endpoint="/admin/manual-order",
                request_hash=request_hash,
            )

    customer_id = _uuid(body.get("customerId"), "customerId")
    customer = await session.get(User, customer_id)
    if customer is None or not customer.is_active or customer.deleted_at is not None:
        raise HTTPException(status_code=404, detail="customer_not_found")
    shipping_address = validate_shipping_address(
        body.get("shippingAddress")
        or body.get("shipping_address")
        or {
            "recipientName": body.get("customerName") or customer.email,
            "phone": body.get("customerPhone"),
            "city": body.get("city"),
            "address": body.get("address"),
        }
    )
    body = {**body, "shippingAddress": shipping_address}
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=400, detail="order_items_required")

    async with session.begin_nested():
        subtotal = Decimal("0.00")
        product_discount = Decimal("0.00")
        lines: list[tuple[Product, ProductVariant | None, int, Decimal]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="invalid_order_item")
            product_id = _uuid(raw.get("productId"), "productId")
            variant_id = _uuid(raw.get("variantId") or raw.get("variant_id"), "variantId") if raw.get("variantId") or raw.get("variant_id") else None
            quantity = parse_strict_quantity(raw.get("quantity") if "quantity" in raw else 1)
            product_result = await session.execute(
                select(Product).where(Product.id == product_id).with_for_update()
            )
            product = product_result.scalar_one_or_none()
            variant = None
            if variant_id:
                variant = (
                    await session.execute(select(ProductVariant).where(ProductVariant.id == variant_id).with_for_update())
                ).scalar_one_or_none()
            line = await eligible_line(session, product=product, variant=variant, variant_id=variant_id, quantity=quantity)
            original_unit = _line_original_unit(line.product, line.variant, line.unit_price)
            subtotal += line_total(original_unit, quantity)
            product_discount += line_total(original_unit - line.unit_price, quantity)
            lines.append((line.product, line.variant, quantity, line.unit_price))

        financials = await calculate_checkout_financials(
            session,
            user_id=customer.id,
            subtotal=subtotal,
            product_discount=product_discount,
            body=body,
        )
        order = Order(
            order_number=f"ADM-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            user_id=customer.id,
            created_by=admin.id,
            status="pending",
            subtotal=financials.subtotal,
            shipping_total=financials.shipping_total,
            discount_total=financials.discount_total,
            total=financials.total,
            currency_code="YER",
            payment_method=payment_method,
            payment_status="pending",
            shipping_address=shipping_address,
            notes=body.get("notes"),
            idempotency_key=str(key) if key else None,
            extra_data={
                "source": "admin_manual",
                "coupon_code": body.get("couponCode"),
                "financial_breakdown": financials.breakdown,
                "coupon_id": financials.coupon_id,
                "shipping_source": financials.shipping_source,
                **({
                    "idempotency_actor_id": str(admin.id),
                    "idempotency_endpoint": "/admin/manual-order",
                    "idempotency_request_hash": request_hash,
                } if key else {}),
            },
        )
        session.add(order)
        await session.flush()
        await _record_financial_side_effects(
            session,
            user_id=customer.id,
            order_id=order.id,
            financials=financials,
        )
        for product, variant, quantity, calculated_unit_price in lines:
            session.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                variant_id=variant.id if variant else None,
                product_name=product.name,
                product_image=product.image_url,
                quantity=quantity,
                unit_price=calculated_unit_price,
                total_price=line_total(calculated_unit_price, quantity),
                partner_id=product.partner_id,
                extra_data={"pricing_snapshot": {"unit_price": str(money(calculated_unit_price)), "quantity": quantity}},
            ))
            if product.track_inventory:
                if variant:
                    variant.stock_quantity -= quantity
                else:
                    product.stock_quantity -= quantity

        history_model = MODEL_BY_TABLE["order_status_history"]
        session.add(history_model(
            order_id=order.id,
            status="pending",
            notes="أنشأ المشرف الطلب يدويًا",
            extra_data={"created_by": str(admin.id)},
        ))
        payment_model = MODEL_BY_TABLE["order_payments"]
        session.add(payment_model(
            order_id=order.id,
            status="pending",
            type=payment_method,
            amount=financials.total,
        ))
        await _create_notification(
            session,
            "notifications",
            user_id=customer.id,
            recipient_id=customer.id,
            order_id=order.id,
            title="تم إنشاء طلبك",
            body=f"أنشأ المشرف الطلب {order.order_number}",
            message=f"أنشأ المشرف الطلب {order.order_number}",
            type="order_created",
            status="new",
            is_read=False,
        )
        audit_model = MODEL_BY_TABLE["audit_logs"]
        session.add(audit_model(
            user_id=admin.id,
            type="manual_order_created",
            description=f"Created order {order.order_number}",
            extra_data={"order_id": str(order.id), "customer_id": str(customer.id)},
        ))
    await session.commit()
    return _serialize_order(order, idempotency_replayed=False if key else None)


@router.get("/orders/stores")
async def ordered_stores(
    limit: int = 50,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(OrderItem, Product, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id, isouter=True)
        .where(Order.user_id == user.id)
        .order_by(Order.created_at.desc())
    )
    summaries: dict[str, dict[str, Any]] = {}
    for item, product, order in result.all():
        partner_id = str(product.partner_id) if product and product.partner_id else "main-store"
        row = summaries.setdefault(
            partner_id,
            {
                "store_id": partner_id,
                "store_name": "\u0627\u0644\u0645\u062a\u062c\u0631 \u0627\u0644\u0631\u0626\u064a\u0633\u064a",
                "_order_ids": set(),
                "items_count": 0,
                "last_order_at": order.created_at.isoformat(),
            },
        )
        row["_order_ids"].add(str(order.id))
        row["items_count"] += item.quantity
        if order.created_at and order.created_at.isoformat() > row["last_order_at"]:
            row["last_order_at"] = order.created_at.isoformat()
    rows = []
    for row in summaries.values():
        order_ids = row.pop("_order_ids", set())
        row["orders_count"] = len(order_ids)
        rows.append(row)
    return rows[:limit]


@router.get("/api/orders/local-shopping")
async def api_user_local_shopping_orders(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return the authenticated customer's local-shopping requests."""
    model = MODEL_BY_TABLE["local_shopping_requests"]
    result = await session.execute(
        select(model)
        .where(model.user_id == user.id, model.deleted_at.is_(None))
        .order_by(model.created_at.desc())
        .limit(500)
    )
    return {"data": await serialize_local_shopping_requests(session, list(result.scalars()))}


@router.get("/api/orders/international-shopping")
async def api_user_international_shopping_orders(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return only the authenticated customer's international orders."""
    model = MODEL_BY_TABLE["international_orders"]
    result = await session.execute(
        select(model)
        .where(model.user_id == user.id, model.deleted_at.is_(None))
        .order_by(model.created_at.desc())
        .limit(500)
    )
    return {"data": [serialize_record(row) for row in result.scalars()]}


@router.get("/orders/{order_id}")
@router.get("/api/orders/{order_id}")
async def order_detail(
    request: Request,
    order_id: uuid.UUID,
    scope: str | None = None,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    is_staff = bool(roles.intersection({"admin", "manager", "finance", "logistics", "staff"}))
    if (scope == "partner" or request.url.path.startswith("/api/partner/")) and "partner" in roles and not is_staff:
        payload = await merchant_order_detail(session, partner_id=user.id, order_id=order_id)
        return {"data": payload} if request.url.path.startswith("/api/") else payload
    identity = (
        await session.execute(
            select(Order.user_id).where(Order.id == order_id, Order.deleted_at.is_(None)).limit(1)
        )
    ).scalar_one_or_none()
    if identity is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    if identity != user.id and not is_staff and "partner" in roles:
        payload = await merchant_order_detail(session, partner_id=user.id, order_id=order_id)
        return {"data": payload} if request.url.path.startswith("/api/") else payload
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    if order.user_id != user.id and not is_staff:
        raise HTTPException(status_code=404, detail="order_not_found")
    items = await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))
    history_model = MODEL_BY_TABLE["order_status_history"]
    history = await session.execute(select(history_model).where(history_model.order_id == order_id).order_by(history_model.created_at))
    payment_model = MODEL_BY_TABLE["order_payments"]
    payments = await session.execute(select(payment_model).where(payment_model.order_id == order_id))
    shipping_model = MODEL_BY_TABLE["order_shipping"]
    shipping = await session.execute(select(shipping_model).where(shipping_model.order_id == order_id).limit(1))
    shipping_row = shipping.scalar_one_or_none()
    payload = {
        "order": _serialize_order(order),
        "items": [serialize_record(row) for row in items.scalars()],
        "payments": [serialize_record(row) for row in payments.scalars()],
        "history": [serialize_record(row) for row in history.scalars()],
        "shipping": serialize_record(shipping_row) if shipping_row is not None else None,
        "shippingHistory": [],
        "notes": order.notes,
    }
    return {"data": payload} if False else payload


@router.get("/api/partner/orders/{order_id}")
async def api_partner_order_detail(
    order_id: uuid.UUID,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if "partner" not in roles:
        raise HTTPException(status_code=403, detail="partner_required")
    return {"data": await merchant_order_detail(session, partner_id=user.id, order_id=order_id)}


@router.patch("/api/partner/orders/{order_id}/status")
async def api_partner_order_status(
    order_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    """Change only the merchant-visible status of an order owned by this partner."""
    if "partner" not in roles:
        raise HTTPException(status_code=403, detail="partner_required")
    body = await request.json()
    next_status = str(body.get("nextStatus") or body.get("status") or "").strip()
    if not next_status:
        raise HTTPException(status_code=400, detail="order_status_required")

    result = await session.execute(
        select(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.id == order_id,
            Order.deleted_at.is_(None),
            OrderItem.partner_id == user.id,
        )
        .with_for_update()
    )
    order = result.unique().scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")

    previous, next_status = assert_allowed_transition(order.status, next_status)
    assert_delivery_proof(next_status, body)
    order.status = next_status
    history_model = MODEL_BY_TABLE["order_status_history"]
    session.add(
        history_model(
            order_id=order.id,
            status=next_status,
            notes=body.get("note"),
            extra_data={"previous_status": previous, "new_status": next_status, "scope": "partner"},
        )
    )
    audit_model = MODEL_BY_TABLE["audit_logs"]
    session.add(
        audit_model(
            user_id=user.id,
            type="partner_order_status_changed",
            description=f"Changed merchant order {order.order_number} status from {previous} to {next_status}",
            extra_data={"order_id": str(order.id), "previous_status": previous, "new_status": next_status},
        )
    )
    await _create_notification(
        session,
        "notifications",
        user_id=order.user_id,
        recipient_id=order.user_id,
        order_id=order.id,
        title="تحديث حالة الطلب",
        body=f"تم تحديث حالة الطلب إلى {next_status}",
        message=f"تم تحديث حالة الطلب إلى {next_status}",
        type="order_status",
        status="new",
        is_read=False,
    )
    await session.commit()
    return {"data": await merchant_order_detail(session, partner_id=user.id, order_id=order_id)}


@router.post("/orders/{order_id}/status")
@router.patch("/api/orders/{order_id}/status")
async def change_order_status(
    order_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if not roles.intersection({"admin", "manager", "logistics", "staff", "employee", "courier", "delivery"}):
        raise HTTPException(status_code=403, detail="insufficient_permissions")
    body = await request.json()
    next_status = str(body.get("nextStatus") or body.get("status") or "").strip()
    if not next_status:
        raise HTTPException(status_code=400, detail="order_status_required")
    result = await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    courier_actor = bool(roles.intersection({"courier", "delivery"}) and not roles.intersection({"admin", "manager", "logistics", "staff", "employee"}))
    if courier_actor:
        assignment_model = MODEL_BY_TABLE["courier_assignments"]
        assignment = (
            await session.execute(
                select(assignment_model).where(
                    assignment_model.order_id == order.id,
                    or_(assignment_model.user_id == user.id, assignment_model.courier_id == user.id),
                    assignment_model.deleted_at.is_(None),
                    assignment_model.status.in_(["active", "assigned", "accepted", "picked_up", "out_for_delivery"]),
                ).with_for_update().limit(1)
            )
        ).scalar_one_or_none()
        if assignment is None:
            raise HTTPException(status_code=403, detail="courier_not_assigned")
    previous, next_status = assert_allowed_transition(order.status, next_status, courier=courier_actor)
    assert_delivery_proof(next_status, body)
    order.status = next_status
    history_model = MODEL_BY_TABLE["order_status_history"]
    session.add(history_model(order_id=order.id, status=next_status, notes=body.get("note"), extra_data={"previous_status": previous, "new_status": next_status}))
    audit_model = MODEL_BY_TABLE["audit_logs"]
    session.add(audit_model(
        user_id=user.id,
        type="order_status_changed",
        description=f"Changed order {order.order_number} status from {previous} to {next_status}",
        extra_data={"order_id": str(order.id), "previous_status": previous, "new_status": next_status},
    ))
    await _create_notification(
        session, "notifications", user_id=order.user_id, recipient_id=order.user_id,
        order_id=order.id, title="تحديث حالة الطلب", body=f"تم تحديث حالة الطلب إلى {next_status}",
        message=f"تم تحديث حالة الطلب إلى {next_status}", type="order_status", status="new", is_read=False,
    )
    await session.commit()
    return serialize_record(order)


@router.post("/api/admin/orders/{order_id}/status/rollback")
async def rollback_order_status(
    order_id: uuid.UUID,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    """Revert only the latest status event through an audited admin action."""
    if not roles.intersection({"admin", "manager"}):
        raise HTTPException(status_code=403, detail="admin_rollback_required")
    order = (
        await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    history_model = MODEL_BY_TABLE["order_status_history"]
    latest = (
        await session.execute(
            select(history_model)
            .where(history_model.order_id == order.id)
            .order_by(history_model.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    current_status = normalize_status(order.status or "pending")
    latest_extra = dict(getattr(latest, "extra_data", None) or {}) if latest is not None else {}
    if latest is None or normalize_status(getattr(latest, "status", "")) != current_status:
        raise HTTPException(status_code=409, detail="no_rollback_available")
    if latest_extra.get("actor") == "admin_rollback":
        raise HTTPException(status_code=409, detail="no_rollback_available")
    previous_status = normalize_status(latest_extra.get("previous_status"))
    if not previous_status or previous_status == current_status:
        raise HTTPException(status_code=409, detail="no_rollback_available")
    order.status = previous_status
    session.add(history_model(
        order_id=order.id,
        status=previous_status,
        notes="Admin rollback of the latest order status",
        extra_data={
            "previous_status": current_status,
            "new_status": previous_status,
            "actor": "admin_rollback",
            "rollback_of": str(latest.id),
        },
    ))
    audit_model = MODEL_BY_TABLE["audit_logs"]
    session.add(audit_model(
        user_id=user.id,
        type="order_status_rolled_back",
        description=f"Rolled back order {order.order_number} status from {current_status} to {previous_status}",
        extra_data={"order_id": str(order.id), "previous_status": current_status, "new_status": previous_status},
    ))
    await session.commit()
    return serialize_record(order)


@router.post("/orders/{order_id}/cancel")
@router.post("/api/orders/{order_id}/cancel")
async def cancel_customer_order(
    order_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Cancel an order owned by the customer before it reaches a terminal state.

    Cancellation is deliberately separate from the staff status endpoint. It
    restores tracked inventory exactly once, records an audit/history entry,
    and leaves financial refund decisions to the finance workflow.
    """
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    order = (
        await session.execute(
            select(Order)
            .where(Order.id == order_id, Order.user_id == user.id, Order.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")

    previous, next_status = assert_allowed_transition(order.status, "cancelled")
    item_rows = list(
        (
            await session.execute(
                select(OrderItem).where(OrderItem.order_id == order.id).with_for_update()
            )
        ).scalars()
    )
    for item in item_rows:
        product = await session.get(Product, item.product_id, with_for_update=True)
        if product is None or not product.track_inventory:
            continue
        if item.variant_id:
            variant = await session.get(ProductVariant, item.variant_id, with_for_update=True)
            if variant is not None:
                variant.stock_quantity += item.quantity
        else:
            product.stock_quantity += item.quantity

    order.status = next_status
    history_model = MODEL_BY_TABLE["order_status_history"]
    session.add(
        history_model(
            order_id=order.id,
            status=next_status,
            notes=body.get("note") or "Customer requested cancellation",
            extra_data={"previous_status": previous, "new_status": next_status, "actor": "customer"},
        )
    )
    audit_model = MODEL_BY_TABLE["audit_logs"]
    session.add(
        audit_model(
            user_id=user.id,
            type="customer_order_cancelled",
            description=f"Customer cancelled order {order.order_number}",
            extra_data={"order_id": str(order.id), "previous_status": previous, "inventory_restored": True},
        )
    )
    await _create_notification(
        session,
        "admin_notifications",
        title="إلغاء طلب",
        body=f"ألغى العميل الطلب {order.order_number}",
        message=f"ألغى العميل الطلب {order.order_number}",
        type="order_cancelled",
        status="new",
        is_read=False,
        extra_data={"order_id": str(order.id), "user_id": str(user.id)},
    )
    await session.commit()
    return serialize_record(order)


@router.get("/orders/stores-legacy", include_in_schema=False)
async def ordered_stores(limit: int = 50, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(OrderItem, Product, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id, isouter=True)
        .where(Order.user_id == user.id)
        .order_by(Order.created_at.desc())
    )
    summaries: dict[str, dict[str, Any]] = {}
    for item, product, order in result.all():
        partner_id = str(product.partner_id) if product and product.partner_id else "main-store"
        row = summaries.setdefault(partner_id, {"store_id": partner_id, "store_name": "المتجر الرئيسي", "_order_ids": set(), "items_count": 0, "last_order_at": order.created_at.isoformat()})
        row["_order_ids"].add(str(order.id))
        row["items_count"] += item.quantity
    rows = []
    for row in summaries.values():
        order_ids = row.pop("_order_ids", set())
        row["orders_count"] = len(order_ids)
        rows.append(row)
    return rows[:limit]


def _require_product_owner(product: Product, user: User, roles: set[str]) -> None:
    if roles.intersection({"admin", "manager", "staff", "employee", "logistics"}):
        return
    if "partner" in roles and product.partner_id == user.id:
        return
    raise HTTPException(status_code=403, detail="product_access_denied")


@router.get("/manage/products")
async def manage_products(
    limit: int = Query(1000, ge=1, le=2000),
    partnerOnly: bool = False,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    if not roles.intersection({"admin", "manager", "partner", "staff", "employee", "logistics"}):
        raise HTTPException(status_code=403, detail="insufficient_permissions")
    statement = select(Product).where(Product.deleted_at.is_(None))
    if partnerOnly or not roles.intersection({"admin", "manager", "staff", "employee", "logistics"}):
        statement = statement.where(Product.partner_id == user.id)
    result = await session.execute(statement.order_by(Product.updated_at.desc()).limit(limit))
    products = list(result.scalars())
    return await _serialize_manage_products(session, products)

@router.get("/manage/brands")
async def manage_brands(
    limit: int = Query(500, ge=1, le=1000),
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    _require_manage_catalog_roles(roles)
    result = await session.execute(
        select(Brand)
        .where(Brand.deleted_at.is_(None), Brand.is_active.is_not(False))
        .order_by(Brand.name)
        .limit(limit)
    )
    return [serialize_record(row) for row in result.scalars()]


@router.get("/manage/suppliers")
async def manage_suppliers(
    limit: int = Query(500, ge=1, le=1000),
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    _require_manage_catalog_roles(roles)
    supplier_model = MODEL_BY_TABLE.get("suppliers")
    if supplier_model is None:
        return []
    result = await session.execute(
        select(supplier_model)
        .where(
            supplier_model.deleted_at.is_(None),
            supplier_model.is_active.is_not(False),
        )
        .order_by(supplier_model.name)
        .limit(limit)
    )
    return [serialize_record(row) for row in result.scalars()]


@router.post("/manage/brands", status_code=201)
async def manage_create_brand(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    _require_manage_catalog_roles(roles)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=422, detail="brand_name_required")
    logo_url = str(body.get("logoUrl") or body.get("logo_url") or "").strip() or None
    brand = Brand(name=name, is_active=True, logo_url=logo_url)
    session.add(brand)
    await session.commit()
    await session.refresh(brand)
    return serialize_record(brand)


@router.post("/manage/suppliers", status_code=201)
async def manage_create_supplier(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    _require_manage_catalog_roles(roles)
    body = await request.json()
    name = str(body.get("name") or body.get("business_name") or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=422, detail="supplier_name_required")
    supplier_model = MODEL_BY_TABLE.get("suppliers")
    if supplier_model is None:
        raise HTTPException(status_code=503, detail="suppliers_unavailable")
    logo_url = str(body.get("logoUrl") or body.get("logo_url") or "").strip() or None
    supplier = supplier_model(name=name, is_active=True)
    if logo_url and hasattr(supplier, "logo_url"):
        supplier.logo_url = logo_url
    session.add(supplier)
    await session.commit()
    await session.refresh(supplier)
    return serialize_record(supplier)


@router.get("/api/catalog/admin/products")
async def catalog_admin_products(
    limit: int = Query(2000, ge=1, le=2000),
    staff: User = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Product).where(Product.deleted_at.is_(None)).order_by(Product.updated_at.desc()).limit(limit)
    )
    rows = await _product_payloads(session, list(result.scalars()), public=False)
    return {"data": rows}


def _product_values(body: dict[str, Any], user: User, roles: set[str], *, partial: bool) -> dict[str, Any]:
    mapping = {
        "name": "name", "nameEn": "name_en", "sku": "sku", "description": "description",
        "richDescription": "rich_description", "price": "price", "originalPrice": "original_price",
        "currencyCode": "currency_code", "stockQuantity": "stock_quantity", "minStockQuantity": "min_stock_quantity",
        "trackInventory": "track_inventory", "isActive": "is_active", "isFeatured": "is_featured",
        "approvalStatus": "approval_status", "approvalNotes": "approval_notes", "categoryId": "category_id",
        "brandId": "brand_id", "supplierId": "supplier_id", "partnerId": "partner_id",
        "imageUrl": "image_url", "images": "images", "tags": "tags", "metaTitle": "meta_title",
        "metaDescription": "meta_description", "promotionalTitle": "promotional_title",
        # The React admin client uses the database-style snake_case names.
        # Keep both contracts valid so create/update behave identically.
        "name_en": "name_en", "rich_description": "rich_description", "original_price": "original_price",
        "currency_code": "currency_code", "stock_quantity": "stock_quantity", "min_stock_quantity": "min_stock_quantity",
        "track_inventory": "track_inventory", "is_active": "is_active", "is_featured": "is_featured",
        "approval_status": "approval_status", "approval_notes": "approval_notes", "category_id": "category_id",
        "brand_id": "brand_id", "supplier_id": "supplier_id", "partner_id": "partner_id",
        "image_url": "image_url", "meta_title": "meta_title", "meta_description": "meta_description",
        "promotional_title": "promotional_title",
    }
    is_merchant = "partner" in roles and not roles.intersection({"admin", "manager"})
    if is_merchant:
        assert_merchant_product_payload_allowed(body)
    values = {target: body[source] for source, target in mapping.items() if source in body}
    for field in ("category_id", "brand_id", "supplier_id", "partner_id"):
        if values.get(field):
            values[field] = _uuid(values[field], field)
    if is_merchant:
        values["partner_id"] = user.id
        values = apply_merchant_product_server_defaults(values)
    return normalize_product_mutation_values(values, partial=partial)


async def _validate_product_references(session: AsyncSession, values: dict[str, Any]) -> None:
    if values.get("category_id"):
        category = await session.get(Category, values["category_id"])
        if category is None or category.deleted_at is not None or category.is_active is False:
            raise HTTPException(status_code=422, detail={"code": "invalid_category", "message": "Category is not available"})
    if values.get("brand_id"):
        brand = await session.get(Brand, values["brand_id"])
        if brand is None or brand.deleted_at is not None or brand.is_active is False:
            raise HTTPException(status_code=422, detail={"code": "invalid_brand", "message": "Brand is not available"})


@router.post("/manage/products", status_code=201)
async def create_product(request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, user.id, roles, "products.create")
    if not roles.intersection({"admin", "manager", "partner"}):
        raise HTTPException(status_code=403, detail="insufficient_permissions")
    body = await request.json()
    values = _product_values(body, user, roles, partial=False)
    values.setdefault("is_active", True)
    if "name" not in values:
        raise HTTPException(status_code=422, detail={"code": "product_name_required", "message": "Product name is required"})
    await _apply_brand_supplier_name_refs(session, body, values)
    await _validate_product_references(session, values)
    product = Product(**values)
    _enforce_product_public_quality(product, roles)
    session.add(product)
    await session.flush()
    if "partner" in roles and not roles.intersection({"admin", "manager"}):
        await NotificationService(session).create_notification(
            NotificationPayload(
                user_id=product.partner_id or user.id,
                title="منتجك قيد المراجعة",
                body=(
                    f"تم حفظ المنتج {product.name} وإرساله للمراجعة والتوثيق. "
                    "ستصلك رسالة عند الموافقة أو الرفض مع السبب."
                ),
                notification_type="product_submitted_for_review",
                category="system",
                priority="high",
                action_type="open_product",
                action_url="/partner/products",
                entity_type="products",
                entity_id=str(product.id),
                payload={
                    "productId": str(product.id),
                    "approvalStatus": str(product.approval_status or "pending"),
                    "deep_link": "/partner/products",
                },
                created_by=user.id,
                deduplication_key=f"product-review:{product.id}:submitted",
            )
        )
    await session.commit()
    return serialize_record(product)


@router.patch("/manage/products/bulk-active")
async def activate_products_bulk(
    request: Request,
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    """Update many product visibility flags in one transaction.

    The mobile admin screen used to issue one HTTP request per product. That
    made a large selection hit the API rate limit and report a misleading
    partial result. The bulk endpoint keeps the mutation atomic and returns a
    count so the client can only report success after the database commit.
    """
    await require_staff_permission(session, user.id, roles, "products.activate")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="product_ids_required")
    raw_ids = body.get("productIds")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(status_code=422, detail="product_ids_required")
    if len(raw_ids) > 2000:
        raise HTTPException(status_code=422, detail="too_many_product_ids")
    product_ids = [_uuid(value, "productIds") for value in raw_ids]
    requested_active = body.get("isActive") is not False
    if requested_active and not roles.intersection(
        {"admin", "manager", "staff", "employee", "logistics"}
    ):
        raise HTTPException(status_code=403, detail="product_activation_admin_only")

    result = await session.execute(
        select(Product).where(
            Product.id.in_(product_ids),
            Product.deleted_at.is_(None),
        )
    )
    products = list(result.scalars())
    if len(products) != len(set(product_ids)):
        raise HTTPException(status_code=404, detail="product_not_found")
    updated_ids: list[str] = []
    failed_products: list[dict[str, str]] = []
    for product in products:
        previous_values = (
            product.is_active,
            product.is_featured,
            product.approval_status,
        )
        try:
            _require_product_owner(product, user, roles)
            product.is_active = requested_active
            if not requested_active:
                product.is_featured = False
                product.approval_status = "inactive"
            _enforce_product_public_quality(product, roles)
        except HTTPException as error:
            product.is_active, product.is_featured, product.approval_status = previous_values
            detail = error.detail
            if isinstance(detail, dict):
                code = str(detail.get("code") or "PRODUCT_UPDATE_FAILED")
                message = str(detail.get("message") or code)
            else:
                code = str(detail or "PRODUCT_UPDATE_FAILED")
                message = code
            failed_products.append(
                {
                    "productId": str(product.id),
                    "code": code,
                    "message": message,
                }
            )
        else:
            updated_ids.append(str(product.id))
    await session.commit()
    return {
        "updatedCount": len(updated_ids),
        "isActive": requested_active,
        "productIds": updated_ids,
        "failedCount": len(failed_products),
        "failedProducts": failed_products,
    }


@router.patch("/manage/products/{product_id}")
async def update_product(product_id: uuid.UUID, request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, user.id, roles, "products.update")
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    _require_product_owner(product, user, roles)
    body = await request.json()
    values = _product_values(body, user, roles, partial=True)
    if "partner" in roles and not roles.intersection({"admin", "manager"}):
        values = apply_merchant_product_server_defaults(values, existing=product)
    await _apply_brand_supplier_name_refs(session, body, values)
    await _validate_product_references(session, values)
    for key, value in values.items():
        setattr(product, key, value)
    _enforce_product_public_quality(product, roles)
    await session.commit()
    return serialize_record(product)


@router.patch("/manage/products/{product_id}/featured")
async def feature_product(product_id: uuid.UUID, request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, user.id, roles, "products.feature")
    if not roles.intersection({"admin", "manager", "staff", "employee", "logistics"}):
        raise HTTPException(status_code=403, detail="featured_admin_only")
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    _require_product_owner(product, user, roles)
    body = await request.json()
    product.is_featured = bool(body.get("isFeatured"))
    _enforce_product_public_quality(product, roles)
    await session.commit()
    return serialize_record(product)


@router.patch("/manage/products/{product_id}/active")
async def activate_product(product_id: uuid.UUID, request: Request, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, user.id, roles, "products.activate")
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    _require_product_owner(product, user, roles)
    body = await request.json()
    requested_active = body.get("isActive") is not False
    if requested_active and not roles.intersection({"admin", "manager", "staff", "employee", "logistics"}):
        raise HTTPException(status_code=403, detail="product_activation_admin_only")
    product.is_active = requested_active
    if not product.is_active:
        product.is_featured = False
        product.approval_status = "inactive"
    _enforce_product_public_quality(product, roles)
    await session.commit()
    return serialize_record(product)


@router.post("/manage/products/{product_id}/disable")
async def disable_product(product_id: uuid.UUID, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, user.id, roles, "products.activate")
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    _require_product_owner(product, user, roles)
    product.is_active = False
    product.is_featured = False
    product.approval_status = "inactive"
    await session.commit()
    return serialize_record(product)


@router.delete("/manage/products/{product_id}")
async def delete_product(product_id: uuid.UUID, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, user.id, roles, "products.delete")
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    _require_product_owner(product, user, roles)
    now = datetime.now(timezone.utc)
    product.is_active = False
    product.is_featured = False
    product.approval_status = "deleted"
    product.deleted_at = now
    variants = list(
        (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == product_id,
                    ProductVariant.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    for variant in variants:
        variant.is_active = False
        variant.deleted_at = now
    removed_assets = await _delete_product_file_assets(
        session,
        product=product,
        variants=variants,
        actor=user,
    )
    await session.commit()
    return {"ok": True, "removed_assets": removed_assets, "data": serialize_record(product)}


@router.get("/manage/products/{product_id}/variants")
async def manage_variants(product_id: uuid.UUID, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    await require_staff_permission(session, user.id, roles, "products.view")
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    _require_product_owner(product, user, roles)
    result = await session.execute(select(ProductVariant).where(ProductVariant.product_id == product_id, ProductVariant.deleted_at.is_(None)))
    return [serialize_record(row) for row in result.scalars()]


@router.post("/manage/products/{product_id}/variants")
async def upsert_variant(product_id: uuid.UUID, request: Request, response: Response, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    _require_product_owner(product, user, roles)
    body = await request.json()
    if body.get("productId") or body.get("product_id"):
        requested_product_id = _uuid(body.get("productId") or body.get("product_id"), "productId")
        if requested_product_id != product_id:
            raise HTTPException(status_code=403, detail="variant_product_mismatch")
    variant = None
    if body.get("id"):
        variant_id = _uuid(body["id"], "id")
        variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.id == variant_id,
                    ProductVariant.product_id == product_id,
                    ProductVariant.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if variant is None:
            raise HTTPException(status_code=404, detail="variant_not_found")
    if variant is None:
        variant = ProductVariant(product_id=product_id)
        session.add(variant)
        response.status_code = 201
    mapping = {"color": "color", "colorHex": "color_hex", "size": "size", "price": "price", "originalPrice": "original_price", "stockQuantity": "stock_quantity", "sku": "sku", "images": "images", "isActive": "is_active", "sortOrder": "sort_order"}
    raw_values = {target: body[source] for source, target in mapping.items() if source in body}
    values = normalize_product_mutation_values(raw_values, partial=True)
    for target, value in values.items():
        setattr(variant, target, value)
    if product.partner_id and not roles.intersection({"admin", "manager"}):
        product.approval_status = "pending"
        product.is_active = False
    await session.commit()
    return serialize_record(variant)


@router.delete("/manage/product-variants/{variant_id}")
async def delete_variant(variant_id: uuid.UUID, user: User = Depends(current_user), roles: set[str] = Depends(user_roles), session: AsyncSession = Depends(get_session)):
    variant = await session.get(ProductVariant, variant_id)
    if variant is None:
        raise HTTPException(status_code=404, detail="variant_not_found")
    product = await session.get(Product, variant.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    _require_product_owner(product, user, roles)
    variant.deleted_at = datetime.now(timezone.utc)
    variant.is_active = False
    await session.commit()
    return {"ok": True}
