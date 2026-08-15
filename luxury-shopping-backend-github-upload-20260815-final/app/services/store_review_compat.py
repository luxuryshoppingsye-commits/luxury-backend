from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

_HANDOVER_PUBLIC_SQL = """
SELECT sr.id, sr.user_id, sr.rating, sr.comment, sr.customer_name,
       sr.is_approved, sr.is_rejected, sr.admin_notes, sr.created_at, sr.updated_at,
       p.full_name AS profile_full_name
FROM public.store_reviews sr
LEFT JOIN public.profiles p ON p.user_id = sr.user_id
WHERE sr.is_approved IS TRUE
ORDER BY sr.created_at DESC
LIMIT :limit
"""

_HANDOVER_MINE_SQL = """
SELECT sr.id, sr.user_id, sr.rating, sr.comment, sr.customer_name,
       sr.is_approved, sr.is_rejected, sr.admin_notes, sr.created_at, sr.updated_at,
       p.full_name AS profile_full_name
FROM public.store_reviews sr
LEFT JOIN public.profiles p ON p.user_id = sr.user_id
WHERE sr.user_id = :user_id
ORDER BY sr.created_at DESC
LIMIT 1
"""

_GENERIC_PUBLIC_SQL = """
SELECT sr.id, sr.user_id, sr.rating,
       COALESCE(sr.comment, sr.body, sr.title) AS comment,
       COALESCE(NULLIF(TRIM(sr.customer_name), ''), NULLIF(TRIM(sr.title), '')) AS customer_name,
       (LOWER(COALESCE(sr.status, '')) = 'approved') AS is_approved,
       (LOWER(COALESCE(sr.status, '')) = 'rejected') AS is_rejected,
       sr.admin_notes, sr.created_at, sr.updated_at, sr.status,
       p.full_name AS profile_full_name
FROM public.store_reviews sr
LEFT JOIN public.profiles p ON p.user_id = sr.user_id
WHERE LOWER(COALESCE(sr.status, '')) = 'approved'
ORDER BY sr.created_at DESC
LIMIT :limit
"""

_GENERIC_MINE_SQL = """
SELECT sr.id, sr.user_id, sr.rating,
       COALESCE(sr.comment, sr.body, sr.title) AS comment,
       COALESCE(NULLIF(TRIM(sr.customer_name), ''), NULLIF(TRIM(sr.title), '')) AS customer_name,
       (LOWER(COALESCE(sr.status, '')) = 'approved') AS is_approved,
       (LOWER(COALESCE(sr.status, '')) = 'rejected') AS is_rejected,
       sr.admin_notes, sr.created_at, sr.updated_at, sr.status,
       p.full_name AS profile_full_name
FROM public.store_reviews sr
LEFT JOIN public.profiles p ON p.user_id = sr.user_id
WHERE sr.user_id = :user_id
ORDER BY sr.created_at DESC
LIMIT 1
"""


def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


_STORE_LABEL_NAMES = frozenset({"المتجر الرئيسي", "Main Store", "المتجر", "Store"})


def normalize_store_review_row(row: dict[str, Any]) -> dict[str, Any]:
    profile_name = str(row.get("profile_full_name") or "").strip()
    customer_name = str(row.get("customer_name") or "").strip()
    if (not customer_name or customer_name in _STORE_LABEL_NAMES) and profile_name:
        customer_name = profile_name
    comment = row.get("comment")
    if comment is None:
        comment = row.get("body") or row.get("title")
    is_approved = row.get("is_approved")
    if is_approved is None:
        is_approved = str(row.get("status") or "").lower() == "approved"
    is_rejected = row.get("is_rejected")
    if is_rejected is None:
        is_rejected = str(row.get("status") or "").lower() == "rejected"
    rating_raw = row.get("rating")
    try:
        rating = int(rating_raw or 0)
    except (TypeError, ValueError):
        rating = 0
    return {
        "id": _json_value(row["id"]),
        "user_id": _json_value(row["user_id"]),
        "rating": max(0, min(5, rating)),
        "comment": str(comment).strip() if comment not in (None, "") else None,
        "customer_name": customer_name or None,
        "is_approved": bool(is_approved),
        "is_rejected": bool(is_rejected),
        "admin_notes": row.get("admin_notes"),
        "created_at": _json_value(row.get("created_at")),
        "updated_at": _json_value(row.get("updated_at")),
    }


async def _fetch_rows(session: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
    try:
        result = await session.execute(text(sql), params)
        return [dict(row) for row in result.mappings().all()]
    except ProgrammingError:
        await session.rollback()
        return None


async def fetch_public_store_reviews(session: AsyncSession, *, limit: int = 20) -> list[dict[str, Any]]:
    params = {"limit": limit}
    rows = await _fetch_rows(session, _HANDOVER_PUBLIC_SQL, params)
    if rows is None:
        rows = await _fetch_rows(session, _GENERIC_PUBLIC_SQL, params)
    if rows is None:
        return []
    return [normalize_store_review_row(row) for row in rows]


async def fetch_user_store_review(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any] | None:
    params = {"user_id": user_id}
    rows = await _fetch_rows(session, _HANDOVER_MINE_SQL, params)
    if rows is None:
        rows = await _fetch_rows(session, _GENERIC_MINE_SQL, params)
    if not rows:
        return None
    return normalize_store_review_row(rows[0])
