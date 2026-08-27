from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.domain import Category
from ..repositories.resources import serialize_record


_CREATE_BLOCKED_FIELDS = {"id", "created_at", "updated_at", "deleted_at", "extra_data"}
_UPDATE_BLOCKED_FIELDS = {"id", "created_at", "deleted_at", "extra_data"}
_CATEGORY_FIELD_ALIASES = {
    "nameEn": "name_en",
    "parentId": "parent_id",
    "imageUrl": "image_url",
    "bannerUrl": "banner_url",
    "bannerTitle": "banner_title",
    "bannerSubtitle": "banner_subtitle",
    "bannerColor": "banner_color",
    "bannerTextColor": "banner_text_color",
    "descriptionAr": "description_ar",
    "descriptionEn": "description_en",
    "sortOrder": "sort_order",
    "isActive": "is_active",
    "isFeatured": "is_featured",
}
_CATEGORY_BOOLEAN_FIELDS = {"is_active", "is_featured"}


def normalize_category_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def normalize_category_mutation_input(body: Any) -> dict[str, Any]:
    """Return one canonical category payload for every admin API contract.

    The dashboard has historically used both camelCase and snake_case names.
    Keeping the conversion at the service boundary prevents partial actions
    such as toggling ``isActive`` from being silently stored in ``extra_data``
    instead of updating the real column.
    """
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_category_payload", "message": "بيانات القسم غير صحيحة"},
        )

    values: dict[str, Any] = {}
    for key, value in body.items():
        canonical_key = _CATEGORY_FIELD_ALIASES.get(key, key)
        # Prefer an explicitly supplied canonical field over its legacy alias.
        if canonical_key in values and key != canonical_key:
            continue
        values[canonical_key] = value

    if "sort_order" in values and values["sort_order"] not in (None, ""):
        raw_sort_order = values["sort_order"]
        try:
            if isinstance(raw_sort_order, bool):
                raise ValueError
            values["sort_order"] = int(str(raw_sort_order).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_category_sort_order", "field": "sort_order", "message": "ترتيب القسم غير صحيح"},
            ) from exc

    for field in _CATEGORY_BOOLEAN_FIELDS:
        if field not in values or isinstance(values[field], bool) or values[field] is None:
            continue
        raw_boolean = str(values[field]).strip().lower()
        if raw_boolean in {"true", "1", "yes", "on"}:
            values[field] = True
        elif raw_boolean in {"false", "0", "no", "off"}:
            values[field] = False
        else:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_category_boolean", "field": field, "message": "قيمة حالة القسم غير صحيحة"},
            )

    return values


def _clean_required_name(value: Any) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    if not cleaned:
        raise HTTPException(status_code=422, detail={"code": "category_name_required", "field": "name"})
    return cleaned


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(value).strip())
    return cleaned or None


def _coerce_uuid(value: Any, *, field: str) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_uuid", "field": field}) from exc


async def _ensure_parent_is_valid(session: AsyncSession, parent_id: uuid.UUID | None, *, current_id: uuid.UUID | None) -> None:
    if parent_id is None:
        return
    if current_id is not None and parent_id == current_id:
        raise HTTPException(status_code=422, detail={"code": "category_parent_cannot_be_self", "field": "parent_id"})
    parent = await session.get(Category, parent_id)
    if parent is None or parent.deleted_at is not None:
        raise HTTPException(status_code=422, detail={"code": "category_parent_not_found", "field": "parent_id"})


async def ensure_category_unique(
    session: AsyncSession,
    *,
    name: str,
    name_en: str | None = None,
    slug: str | None,
    exclude_id: uuid.UUID | None = None,
) -> None:
    normalized_name = normalize_category_text(name)
    name_query = select(Category.id).where(
        Category.deleted_at.is_(None),
        func.lower(func.btrim(Category.name)) == normalized_name,
    )
    if exclude_id is not None:
        name_query = name_query.where(Category.id != exclude_id)
    duplicate_name = (await session.execute(name_query.limit(1))).scalar_one_or_none()
    if duplicate_name is not None:
        raise HTTPException(status_code=409, detail={"code": "duplicate_category_name", "field": "name"})

    normalized_name_en = normalize_category_text(name_en) if name_en else None
    if normalized_name_en:
        name_en_query = select(Category.id).where(
            Category.deleted_at.is_(None),
            Category.name_en.is_not(None),
            func.lower(func.btrim(Category.name_en)) == normalized_name_en,
        )
        if exclude_id is not None:
            name_en_query = name_en_query.where(Category.id != exclude_id)
        duplicate_name_en = (await session.execute(name_en_query.limit(1))).scalar_one_or_none()
        if duplicate_name_en is not None:
            raise HTTPException(status_code=409, detail={"code": "duplicate_category_name_en", "field": "name_en"})

    normalized_slug = normalize_category_text(slug) if slug else None
    if normalized_slug:
        slug_query = select(Category.id).where(
            Category.deleted_at.is_(None),
            Category.slug.is_not(None),
            func.lower(func.btrim(Category.slug)) == normalized_slug,
        )
        if exclude_id is not None:
            slug_query = slug_query.where(Category.id != exclude_id)
        duplicate_slug = (await session.execute(slug_query.limit(1))).scalar_one_or_none()
        if duplicate_slug is not None:
            raise HTTPException(status_code=409, detail={"code": "duplicate_category_slug", "field": "slug"})


def _category_values(body: dict[str, Any], *, blocked_fields: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    columns = Category.__table__.c
    values = {
        key: value
        for key, value in body.items()
        if key in columns and key not in blocked_fields
    }
    extra = {
        key: value
        for key, value in body.items()
        if key not in columns
    }
    if "name" in values:
        values["name"] = _clean_required_name(values["name"])
    if "name_en" in values:
        values["name_en"] = _clean_optional_text(values["name_en"])
    if "slug" in values:
        slug = _clean_optional_text(values["slug"])
        values["slug"] = slug.lower() if slug else None
    if "parent_id" in values:
        values["parent_id"] = _coerce_uuid(values["parent_id"], field="parent_id")
    return values, extra


async def create_category_record(session: AsyncSession, body: dict[str, Any]) -> dict[str, Any]:
    body = normalize_category_mutation_input(body)
    values, extra = _category_values(body, blocked_fields=_CREATE_BLOCKED_FIELDS)
    if "name" not in values:
        values["name"] = _clean_required_name(None)
    await _ensure_parent_is_valid(session, values.get("parent_id"), current_id=None)
    await ensure_category_unique(session, name=values["name"], name_en=values.get("name_en"), slug=values.get("slug"))
    row = Category(**values, extra_data=extra)
    session.add(row)
    await session.flush()
    return serialize_record(row)


async def update_category_record(session: AsyncSession, category_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    body = normalize_category_mutation_input(body)
    row = await session.get(Category, category_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="category_not_found")
    values, extra_updates = _category_values(body, blocked_fields=_UPDATE_BLOCKED_FIELDS)
    name = values.get("name", row.name)
    name_en = values.get("name_en", row.name_en)
    slug = values.get("slug", row.slug)
    parent_id = values.get("parent_id", row.parent_id)
    await _ensure_parent_is_valid(session, parent_id, current_id=row.id)
    await ensure_category_unique(session, name=name, name_en=name_en, slug=slug, exclude_id=row.id)

    extra = dict(row.extra_data or {})
    for key, value in values.items():
        setattr(row, key, value)
    for key, value in extra_updates.items():
        extra[key] = value
    row.extra_data = extra
    await session.flush()
    return serialize_record(row)


async def soft_delete_category_record(session: AsyncSession, category_id: uuid.UUID) -> None:
    row = await session.get(Category, category_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="category_not_found")
    now = datetime.now(timezone.utc)
    row.deleted_at = now
    row.updated_at = now
    row.is_active = False
    await session.flush()
