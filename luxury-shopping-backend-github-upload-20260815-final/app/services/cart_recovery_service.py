from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MODEL_BY_TABLE
from ..models.domain import Order, User, UserCart
from .notification_service import NotificationPayload, NotificationService

REMINDER_AFTER = timedelta(hours=1)
OFFER_AFTER = timedelta(hours=8)
OFFER_VALID_FOR = timedelta(hours=24)


def recovery_stage(activity: datetime, now: datetime) -> str | None:
    age = now - activity
    if age >= OFFER_AFTER:
        return "cart_discount"
    return "cart_reminder" if age >= REMINDER_AFTER else None


async def cart_recovery_is_current(session: AsyncSession, user_id: Any, payload: dict) -> bool:
    try:
        activity = datetime.fromisoformat(str(payload["cart_activity"]))
        expires = datetime.fromisoformat(str(payload["recovery_expires_at"]))
    except (KeyError, ValueError, TypeError):
        return False
    if activity.tzinfo is None or expires.tzinfo is None or expires <= datetime.now(timezone.utc):
        return False
    latest = (await session.execute(select(func.max(UserCart.updated_at)).where(UserCart.user_id == user_id))).scalar_one_or_none()
    if latest != activity:
        return False
    ordered = (await session.execute(select(Order.id).where(Order.user_id == user_id, Order.deleted_at.is_(None), Order.created_at >= activity).limit(1))).scalar_one_or_none()
    return ordered is None


async def process_cart_recovery(session: AsyncSession, *, now: datetime | None = None, limit: int = 50) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    # One transaction owns each scan even when several web workers are running.
    locked = (await session.execute(select(func.pg_try_advisory_xact_lock(6040906)))).scalar_one()
    if not locked:
        return {"total": 0}
    notices = MODEL_BY_TABLE["notifications"]
    preferences = MODEL_BY_TABLE["notification_preferences"]
    carts = select(UserCart.user_id.label("user_id"), func.max(UserCart.updated_at).label("activity")).group_by(UserCart.user_id).subquery()
    def sent(kind: str):
        return select(notices.id).where(notices.user_id == carts.c.user_id, notices.type == kind, notices.created_at >= carts.c.activity).exists()
    ordered = select(Order.id).where(Order.user_id == carts.c.user_id, Order.deleted_at.is_(None), Order.created_at >= carts.c.activity).exists()
    opted_out = select(preferences.id).where(preferences.user_id == carts.c.user_id, preferences.deleted_at.is_(None), preferences.promotional_notifications.is_(False)).exists()
    candidates = (await session.execute(
        select(carts.c.user_id, carts.c.activity).join(User, User.id == carts.c.user_id).where(
            User.is_active.is_(True), User.deleted_at.is_(None),
            carts.c.activity <= now - REMINDER_AFTER, ~ordered, ~opted_out, ~sent("cart_discount"),
            (carts.c.activity <= now - OFFER_AFTER) | ~sent("cart_reminder"),
        ).order_by(carts.c.activity).limit(limit)
    )).all()
    service = NotificationService(session)
    count = 0
    for user_id, activity in candidates:
        # Re-read after candidate selection; checkout or a cart edit may have completed.
        current = (await session.execute(select(func.max(UserCart.updated_at)).where(UserCart.user_id == user_id))).scalar_one_or_none()
        if current != activity:
            continue
        if (await session.execute(select(Order.id).where(Order.user_id == user_id, Order.deleted_at.is_(None), Order.created_at >= activity).limit(1))).scalar_one_or_none():
            continue
        kind = recovery_stage(activity, now)
        if kind is None:
            continue
        dedupe = f"cart-recovery:{user_id}:{activity.isoformat()}:{kind}"
        existing = (await session.execute(select(notices.id).where(notices.deduplication_key == dedupe).limit(1))).scalar_one_or_none()
        if existing:
            continue
        expires = now + OFFER_VALID_FOR
        payload = {"cart_activity": activity.isoformat(), "recovery_expires_at": expires.isoformat(), "deep_link": "/cart"}
        title = "منتجاتك تنتظرك في السلة"
        body = "اخترت منتجات جميلة ولم تكمل طلبك بعد. ارجع إلى سلتك وأكمل التسوق وقتما يناسبك."
        if kind == "cart_discount":
            code = "CART" + secrets.token_hex(6).upper()
            coupon = MODEL_BY_TABLE["coupons"](
                code=code, title="توصيل مجاني خاص بسلتك", status="active", is_active=True, amount=0,
                expires_at=expires, extra_data={"discount_type": "free_shipping", "exclusive_user_id": str(user_id),
                    "usage_limit": 1, "per_user_limit": 1, "uses_per_user": 1, "source": "cart_recovery"},
            )
            session.add(coupon)
            await session.flush()
            payload["coupon_code"] = code
            title = "توصيل مجاني حصري لك"
            body = f"أكمل طلب سلتك بكود {code} واحصل على توصيل مجاني. الكود خاص بحسابك، صالح لمدة 24 ساعة ولمرة واحدة."
        await service.create_notification(NotificationPayload(
            user_id=user_id, title=title, body=body, notification_type=kind, category="promotional",
            action_type="open_cart", action_url="/cart", payload=payload, deduplication_key=dedupe,
            expires_at=expires, delivery_channels=("in_app", "mobile_push", "web_push"),
        ))
        count += 1
    return {"total": count}
