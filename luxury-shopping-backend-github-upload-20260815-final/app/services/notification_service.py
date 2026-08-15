from __future__ import annotations

import hashlib
import json
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE
from ..repositories.resources import serialize_record
from .firebase_auth_service import ensure_firebase_admin_app
from .outbox_service import email_delivery_configured
from .realtime import RealtimeEventService, realtime_hub

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except ImportError:  # pragma: no cover - exercised when optional dependency is not installed
    firebase_admin = None
    credentials = None
    messaging = None

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - exercised when optional dependency is not installed
    WebPushException = Exception
    webpush = None


SECURITY_CATEGORIES = {"security", "account_security", "password", "login"}
CLAIMABLE_OUTBOX_STATUSES = {"pending", "queued", "failed_retryable"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _safe_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (payload or {}).items() if "token" not in key.lower() and "secret" not in key.lower()}


def _clean_notification_text(value: Any, fallback: str) -> str:
    """Keep broken legacy Arabic or replacement-question-mark text off push."""
    text = str(value or "").strip()
    for _ in range(3):
        try:
            repaired = text.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if repaired == text:
            break
        text = repaired
    question_marks = text.count("?") + text.count("؟")
    if not text or "???" in text or "؟؟؟" in text or (question_marks >= 2 and question_marks * 2 >= len(text)):
        return fallback
    return text


def _extra(row: Any) -> dict[str, Any]:
    return dict(getattr(row, "extra_data", None) or {})


def _set_extra(row: Any, values: dict[str, Any]) -> None:
    row.extra_data = {**_extra(row), **values}


def _retry_delay(attempts: int) -> timedelta:
    settings = get_settings()
    exponent = max(attempts - 1, 0)
    base = min(settings.message_retry_base_seconds * (2 ** exponent), settings.message_retry_max_seconds)
    jitter = random.randint(0, max(1, settings.message_retry_base_seconds))
    return timedelta(seconds=min(base + jitter, settings.message_retry_max_seconds))


@dataclass(frozen=True)
class NotificationPayload:
    user_id: uuid.UUID
    title: str
    body: str
    notification_type: str = "message"
    category: str = "system"
    priority: str = "normal"
    image_url: str | None = None
    action_type: str | None = None
    action_url: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] | None = None
    created_by: uuid.UUID | None = None
    source: str = "fastapi"
    deduplication_key: str | None = None
    expires_at: datetime | None = None


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def preferences_for(self, user_id: uuid.UUID) -> Any:
        model = MODEL_BY_TABLE["notification_preferences"]
        row = (
            await self.session.execute(
                select(model).where(model.user_id == user_id, model.deleted_at.is_(None)).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = model(user_id=user_id, status="active")
            self.session.add(row)
            await self.session.flush()
        return row

    async def update_preferences(self, user_id: uuid.UUID, values: dict[str, Any]) -> dict[str, Any]:
        row = await self.preferences_for(user_id)
        for key in (
            "in_app_enabled",
            "mobile_push_enabled",
            "web_push_enabled",
            "order_updates",
            "payment_updates",
            "shipping_updates",
            "promotional_notifications",
            "support_updates",
            "system_notifications",
        ):
            if key in values:
                setattr(row, key, bool(values[key]))
        if "security_notifications" in values:
            row.security_notifications = True
        return serialize_record(row)

    async def create_notification(self, payload: NotificationPayload, *, enqueue: bool = True) -> Any:
        model = MODEL_BY_TABLE["notifications"]
        if payload.deduplication_key:
            existing = (
                await self.session.execute(
                    select(model)
                    .where(model.deduplication_key == payload.deduplication_key, model.deleted_at.is_(None))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        row = model(
            user_id=payload.user_id,
            recipient_id=payload.user_id,
            title=payload.title,
            body=payload.body,
            message=payload.body,
            type=payload.notification_type,
            notification_type=payload.notification_type,
            category=payload.category,
            priority=payload.priority,
            image_url=payload.image_url,
            action_type=payload.action_type,
            url=payload.action_url,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            payload=_safe_payload(payload.payload),
            status="new",
            is_read=False,
            created_by=payload.created_by,
            source=payload.source,
            deduplication_key=payload.deduplication_key,
            expires_at=payload.expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        realtime_event = await RealtimeEventService().record_event(
            self.session,
            channel=f"user:{payload.user_id}",
            event="notification.created",
            payload=serialize_record(row),
            dedupe_key=f"notification.created:{row.id}",
            user_id=payload.user_id,
        )
        await realtime_hub.publish_recorded_event(
            f"user:{payload.user_id}",
            {
                "event": "notification.created",
                "type": "notification.created",
                "payload": serialize_record(row),
                "event_id": realtime_event.get("event_id") or realtime_event.get("id"),
                "channel": f"user:{payload.user_id}",
            },
        )
        if enqueue:
            await self.enqueue_notification(row, payload)
        return row

    async def create_bulk_notifications(self, payloads: list[NotificationPayload]) -> list[Any]:
        created = []
        for payload in payloads:
            created.append(await self.create_notification(payload))
        return created

    async def enqueue_notification(self, notification: Any, payload: NotificationPayload) -> Any:
        model = MODEL_BY_TABLE["notification_outbox"]
        if payload.deduplication_key:
            existing = (
                await self.session.execute(
                    select(model)
                    .where(
                        model.extra_data["dedupe_key"].astext == payload.deduplication_key,
                        model.status.notin_(("cancelled", "expired")),
                        model.deleted_at.is_(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        event_id = uuid.uuid4()
        row = model(
            event_id=event_id,
            event_type=f"notification.{payload.notification_type}",
            aggregate_type=payload.entity_type or "notification",
            aggregate_id=uuid.UUID(payload.entity_id) if payload.entity_id and _is_uuid(payload.entity_id) else None,
            user_id=payload.user_id,
            payload={"notification_id": str(notification.id), **_safe_payload(payload.payload)},
            status="queued",
            attempts=0,
            available_at=_now(),
            type=payload.notification_type,
            title=payload.title,
            message=payload.body,
            extra_data={
                "priority": payload.priority,
                "category": payload.category,
                "dedupe_key": payload.deduplication_key,
                "max_attempts": get_settings().message_max_attempts,
                "source": payload.source,
            },
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def mark_notification_read(self, notification_id: uuid.UUID, user_id: uuid.UUID) -> Any:
        model = MODEL_BY_TABLE["notifications"]
        row = await self.session.get(model, notification_id)
        if row is None or (row.user_id != user_id and row.recipient_id != user_id):
            return None
        row.is_read = True
        row.read_at = _now()
        realtime_event = await RealtimeEventService().record_event(
            self.session,
            channel=f"user:{user_id}",
            event="notification.read",
            payload={"id": str(row.id)},
            dedupe_key=f"notification.read:{row.id}:{row.read_at.isoformat()}",
            user_id=user_id,
        )
        await realtime_hub.publish_recorded_event(
            f"user:{user_id}",
            {
                "event": "notification.read",
                "type": "notification.read",
                "payload": {"id": str(row.id)},
                "event_id": realtime_event.get("event_id") or realtime_event.get("id"),
                "channel": f"user:{user_id}",
            },
        )
        return row

    async def mark_all_notifications_read(self, user_id: uuid.UUID) -> int:
        model = MODEL_BY_TABLE["notifications"]
        rows = (
            await self.session.execute(
                select(model).where(or_(model.user_id == user_id, model.recipient_id == user_id), model.is_read.is_(False))
            )
        ).scalars().all()
        now = _now()
        for row in rows:
            row.is_read = True
            row.read_at = now
        realtime_event = await RealtimeEventService().record_event(
            self.session,
            channel=f"user:{user_id}",
            event="notifications.read_all",
            payload={"updated": len(rows)},
            dedupe_key=f"notifications.read_all:{user_id}:{now.isoformat()}",
            user_id=user_id,
        )
        await realtime_hub.publish_recorded_event(
            f"user:{user_id}",
            {
                "event": "notifications.read_all",
                "type": "notifications.read_all",
                "payload": {"updated": len(rows)},
                "event_id": realtime_event.get("event_id") or realtime_event.get("id"),
                "channel": f"user:{user_id}",
            },
        )
        return len(rows)

    async def get_unread_count(self, user_id: uuid.UUID) -> int:
        model = MODEL_BY_TABLE["notifications"]
        return int(
            (
                await self.session.execute(
                    select(func.count()).select_from(model).where(
                        or_(model.user_id == user_id, model.recipient_id == user_id),
                        model.is_read.is_(False),
                        model.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
        )

    async def register_device_token(self, user_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
        token = str(body.get("token") or body.get("deviceToken") or "").strip()
        if not token:
            raise ValueError("token_required")
        platform = str(body.get("platform") or "android").lower()
        device_id = str(body.get("deviceId") or body.get("device_id") or "").strip() or None
        model = MODEL_BY_TABLE["push_tokens"]
        existing_rows = (
            await self.session.execute(select(model).where(model.token == token).with_for_update())
        ).scalars().all()
        row = existing_rows[0] if existing_rows else model(user_id=user_id, token=token)
        for duplicate in existing_rows[1:]:
            duplicate.is_active = False
            duplicate.status = "duplicate_inactive"
            duplicate.invalidated_at = _now()
        if device_id:
            device_rows = (
                await self.session.execute(
                    select(model).where(
                        model.user_id == user_id,
                        model.device_id == device_id,
                        model.platform == platform,
                        model.token != token,
                        model.is_active.is_(True),
                    ).with_for_update()
                )
            ).scalars().all()
            for duplicate in device_rows:
                duplicate.is_active = False
                duplicate.status = "refreshed_inactive"
                duplicate.invalidated_at = _now()
        row.user_id = user_id
        row.platform = platform
        row.device_id = device_id
        row.app_version = str(body.get("appVersion") or body.get("app_version") or "") or None
        row.device_name = str(body.get("deviceName") or body.get("device_name") or "") or None
        row.environment = str(body.get("environment") or get_settings().app_env)
        row.status = "active"
        row.is_active = True
        row.last_seen_at = _now()
        if not existing_rows:
            self.session.add(row)
        await self.session.flush()
        data = serialize_record(row)
        data["token_ref"] = _token_ref(token)
        data.pop("token", None)
        return data

    async def unregister_device_token(self, user_id: uuid.UUID, token: str) -> bool:
        model = MODEL_BY_TABLE["push_tokens"]
        row = (
            await self.session.execute(select(model).where(model.user_id == user_id, model.token == token).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return False
        row.is_active = False
        row.status = "inactive"
        row.invalidated_at = _now()
        return True

    async def register_web_push_subscription(self, user_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
        endpoint = str(body.get("endpoint") or "").strip()
        keys = body.get("keys") or {}
        p256dh = str(body.get("p256dh") or keys.get("p256dh") or "").strip()
        auth = str(body.get("auth") or keys.get("auth") or "").strip()
        if not endpoint or not p256dh or not auth:
            raise ValueError("subscription_required")
        model = MODEL_BY_TABLE["web_push_subscriptions"]
        rows = (
            await self.session.execute(select(model).where(model.endpoint == endpoint).with_for_update())
        ).scalars().all()
        row = rows[0] if rows else None
        for duplicate in rows[1:]:
            duplicate.is_active = False
            duplicate.status = "duplicate_inactive"
            duplicate.invalidated_at = _now()
        if row is None:
            row = model(user_id=user_id, endpoint=endpoint)
            self.session.add(row)
        row.user_id = user_id
        row.p256dh = p256dh
        row.auth = auth
        row.browser = str(body.get("browser") or "")[:120] or None
        row.user_agent = str(body.get("userAgent") or body.get("user_agent") or "")[:500] or None
        row.status = "active"
        row.is_active = True
        await self.session.flush()
        data = serialize_record(row)
        data["endpoint_ref"] = _token_ref(endpoint)
        data.pop("endpoint", None)
        data.pop("p256dh", None)
        data.pop("auth", None)
        return data

    async def process_outbox_once(self, *, limit: int = 50) -> dict[str, int]:
        settings = get_settings()
        limit = min(max(limit, 1), settings.message_batch_size)
        outbox_model = MODEL_BY_TABLE["notification_outbox"]
        rows = (
            await self.session.execute(
                select(outbox_model)
                .where(
                    outbox_model.status.in_(CLAIMABLE_OUTBOX_STATUSES),
                    outbox_model.available_at <= _now(),
                    outbox_model.deleted_at.is_(None),
                )
                .order_by(outbox_model.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        processed = failed = retry_scheduled = dead_letter = blocked = expired = 0
        now = _now()
        for row in rows:
            extra = _extra(row)
            expires_at = extra.get("expires_at")
            if isinstance(expires_at, str):
                try:
                    expires_at_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except ValueError:
                    expires_at_dt = None
            else:
                expires_at_dt = expires_at if isinstance(expires_at, datetime) else None
            if expires_at_dt and expires_at_dt <= now:
                row.status = "expired"
                row.last_error = "message_expired"
                _set_extra(row, {"last_error_code": "message_expired", "processed_at": now.isoformat()})
                expired += 1
                continue
            row.status = "processing"
            _set_extra(
                row,
                {
                    "locked_at": now.isoformat(),
                    "lock_expires_at": (now + timedelta(seconds=settings.message_lock_timeout_seconds)).isoformat(),
                    "worker": "message_worker",
                },
            )
            await self.session.flush()
            row.attempts = int(row.attempts or 0) + 1
            try:
                # Provider/delivery bookkeeping errors must not abort the
                # surrounding transaction and freeze every later message.
                async with self.session.begin_nested():
                    result = await self._deliver_outbox(row)
            except Exception as exc:
                failed += 1
                if row.attempts >= settings.message_max_attempts:
                    row.status = "dead_letter"
                    dead_letter += 1
                else:
                    row.status = "failed_retryable"
                    retry_scheduled += 1
                row.available_at = _now() + _retry_delay(row.attempts)
                detail = " ".join(str(exc).split())[:160]
                if any(word in detail.lower() for word in ("token", "password", "secret", "credential")):
                    detail = ""
                row.last_error = f"delivery_exception:{exc.__class__.__name__}" + (f":{detail}" if detail else "")
                _set_extra(
                    row,
                    {
                        "last_error_code": row.last_error,
                        "locked_at": None,
                        "lock_expires_at": None,
                        "next_attempt_at": row.available_at.isoformat(),
                    },
                )
                continue
            if result.get("suppressed"):
                row.status = "suppressed_by_preference"
                row.processed_at = _now()
                _set_extra(row, {"last_error_code": result["error"], "processed_at": row.processed_at.isoformat(), "locked_at": None, "lock_expires_at": None})
                processed += 1
            elif result["blocked"] and not result["ok"]:
                row.status = "blocked_configuration"
                row.last_error = result["error"]
                _set_extra(row, {"last_error_code": result["error"], "processed_at": _now().isoformat(), "locked_at": None, "lock_expires_at": None})
                blocked += 1
            elif result["ok"]:
                row.status = "processed"
                row.processed_at = _now()
                _set_extra(row, {"processed_at": row.processed_at.isoformat(), "locked_at": None, "lock_expires_at": None})
                processed += 1
            else:
                failed += 1
                if row.attempts >= settings.message_max_attempts:
                    row.status = "dead_letter"
                    dead_letter += 1
                else:
                    row.status = "failed_retryable"
                    retry_scheduled += 1
                row.available_at = _now() + _retry_delay(row.attempts)
                row.last_error = result["error"]
                _set_extra(
                    row,
                    {
                        "last_error_code": result["error"],
                        "last_error_safe": str(result["error"] or "")[:300],
                        "next_attempt_at": row.available_at.isoformat(),
                        "locked_at": None,
                        "lock_expires_at": None,
                        "max_attempts": settings.message_max_attempts,
                    },
                )
        return {
            "processed": processed,
            "failed": failed,
            "retry_scheduled": retry_scheduled,
            "dead_letter": dead_letter,
            "blocked": blocked,
            "expired": expired,
            "total": len(rows),
        }

    async def _deliver_outbox(self, row: Any) -> dict[str, Any]:
        notification_id = (row.payload or {}).get("notification_id")
        channels = await self._allowed_channels(row.user_id, row.type or "message")
        if not channels:
            return {"ok": True, "blocked": False, "suppressed": True, "error": "communication_suppressed"}
        ok = True
        blocked = False
        errors = []
        for channel in channels:
            status = await self._deliver_channel(row, notification_id, channel)
            blocked = blocked or status == "blocked_configuration"
            ok = ok and status in {"sent", "provider_accepted", "blocked_configuration"}
            if status not in {"sent", "provider_accepted", "blocked_configuration"}:
                errors.append(f"{channel}:{status}")
        return {"ok": ok, "blocked": blocked and not ok, "suppressed": False, "error": None if ok or blocked else ",".join(errors) or "delivery_failed"}

    async def _allowed_channels(self, user_id: uuid.UUID, notification_type: str) -> list[str]:
        pref = await self.preferences_for(user_id)
        category = _category_from_type(notification_type)
        channels = []
        if pref.in_app_enabled:
            channels.append("in_app")
        if pref.mobile_push_enabled and _category_allowed(pref, category):
            channels.append("mobile_push")
        if pref.web_push_enabled and _category_allowed(pref, category):
            channels.append("web_push")
        channels.append("email")
        if category in SECURITY_CATEGORIES:
            for required in ("in_app", "mobile_push", "web_push"):
                if required not in channels:
                    channels.append(required)
        return channels

    async def _deliver_channel(self, row: Any, notification_id: str | None, channel: str) -> str:
        if channel == "in_app":
            return await self._record_delivery(row.user_id, notification_id, channel, "database", "sent", response_code="stored")
        if channel == "mobile_push":
            return await self._send_mobile_push(row, notification_id)
        if channel == "web_push":
            return await self._send_web_push(row, notification_id)
        if channel == "email":
            return await self._queue_email(row, notification_id)
        return await self._record_delivery(row.user_id, notification_id, channel, "unknown", "failed", error_code="unknown_channel")

    async def _queue_email(self, row: Any, notification_id: str | None) -> str:
        user_model = MODEL_BY_TABLE["users"]
        user = await self.session.get(user_model, row.user_id)
        email = str(getattr(user, "email", "") or "").strip()
        if not email:
            return await self._record_delivery(row.user_id, notification_id, "email", "smtp", "failed", error_code="recipient_email_missing")
        email_model = MODEL_BY_TABLE["email_outbox"]
        existing = None
        if notification_id:
            existing = (
                await self.session.execute(
                    select(email_model).where(
                        email_model.user_id == row.user_id,
                        email_model.extra_data["notification_id"].astext == str(notification_id),
                        email_model.deleted_at.is_(None),
                    ).limit(1)
                )
            ).scalar_one_or_none()
        if existing is None:
            email_row = email_model(
                user_id=row.user_id,
                title=row.title or "رفاهية التسوق",
                status="pending",
                email=email,
                message=row.message or row.body or "",
                extra_data={
                    "notification_id": str(notification_id or ""),
                    "category": _category_from_type(row.type or "message"),
                    "notification_type": row.type or "message",
                },
            )
            self.session.add(email_row)
            await self.session.flush()
        settings = get_settings()
        if not email_delivery_configured(settings):
            return await self._record_delivery(
                row.user_id,
                notification_id,
                "email",
                "email_provider",
                "blocked_configuration",
                error_code="email_delivery_configuration_required",
            )
        return await self._record_delivery(
            row.user_id,
            notification_id,
            "email",
            "email_provider",
            "sent",
            target=email,
            response_code="queued",
        )

    async def _send_mobile_push(self, row: Any, notification_id: str | None) -> str:
        settings = get_settings()
        if firebase_admin is None or messaging is None or credentials is None:
            return await self._record_delivery(row.user_id, notification_id, "mobile_push", "firebase_admin", "blocked_configuration", error_code="firebase_admin_not_installed")
        has_firebase_credentials = bool(
            settings.google_application_credentials
            or settings.google_application_credentials_json
            or settings.firebase_service_account_json
        )
        if not (settings.firebase_project_id and has_firebase_credentials):
            return await self._record_delivery(row.user_id, notification_id, "mobile_push", "firebase_admin", "blocked_configuration", error_code="firebase_credentials_required")
        init_error = _ensure_firebase_app(settings)
        if init_error:
            return await self._record_delivery(row.user_id, notification_id, "mobile_push", "firebase_admin", "blocked_configuration", error_code=init_error)
        token_model = MODEL_BY_TABLE["push_tokens"]
        tokens = (
            await self.session.execute(
                select(token_model).where(
                    token_model.user_id == row.user_id,
                    token_model.is_active.is_(True),
                    token_model.status == "active",
                    token_model.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not tokens:
            return await self._record_delivery(row.user_id, notification_id, "mobile_push", "firebase_admin", "blocked_configuration", error_code="no_active_tokens")
        notification = self._notification_payload(row, notification_id)
        notification_data = {
            key: str(value)
            for key, value in notification["data"].items()
            if value is not None
        }
        notification_data.setdefault("notification_id", str(notification_id or ""))
        # Android handles data-only messages through the app background
        # handler, which keeps Arabic text and the branded large icon intact.
        notification_data.setdefault("title", notification["title"])
        notification_data.setdefault("body", notification["body"])
        notification_data.setdefault("message", notification["body"])
        notification_data.setdefault("click_action", "FLUTTER_NOTIFICATION_CLICK")
        notification_data.setdefault("site_url", settings.frontend_public_url)
        deep_link = str(notification_data.get("deep_link") or notification["url"] or settings.frontend_public_url).strip()
        if not deep_link.startswith(("http://", "https://")):
            deep_link = f"{settings.frontend_public_url.rstrip('/')}/{deep_link.lstrip('/')}"
        sent = failed = invalid = 0
        last_error = None
        for token_row in tokens:
            token = getattr(token_row, "token", None)
            if not token:
                continue
            platform = str(getattr(token_row, "platform", "") or "").lower()
            android_notification = None if platform == "android" else messaging.AndroidNotification(
                channel_id="luxury_notifications",
                icon="ic_notification",
                color="#9A6A05",
                sound="default",
                click_action="FLUTTER_NOTIFICATION_CLICK",
            )
            message = messaging.Message(
                token=token,
                # Android is data-only so Flutter owns the Arabic rendering
                # and can show the exact app branding in the expanded card.
                notification=None if platform == "android" else messaging.Notification(
                    title=notification["title"],
                    body=notification["body"],
                ),
                data=notification_data,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=android_notification,
                ),
                apns=messaging.APNSConfig(
                    headers={"apns-priority": "10"},
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            badge=1,
                            sound="default",
                            category="LUXURY_NOTIFICATION",
                        )
                    ),
                ),
                webpush=messaging.WebpushConfig(
                    notification=messaging.WebpushNotification(
                        title=notification["title"],
                        body=notification["body"],
                        icon="/logo.png",
                        badge="/favicon.png",
                        tag=notification["tag"],
                        require_interaction=False,
                    ),
                    fcm_options=messaging.WebpushFCMOptions(link=deep_link),
                ),
            )
            try:
                message_id = messaging.send(message)
                sent += 1
                await self._record_delivery(row.user_id, notification_id, "mobile_push", "firebase_admin", "provider_accepted", target=f"fcm:{_token_ref(token)}", response_code=message_id)
            except Exception as exc:  # Firebase Admin raises provider-specific subclasses.
                failed += 1
                error_detail = " ".join(str(exc).split())
                if token and error_detail:
                    error_detail = error_detail.replace(token, "[redacted]")
                last_error = exc.__class__.__name__
                safe_error_code = f"{last_error}:{error_detail[:180]}" if error_detail else last_error
                if last_error in {"UnregisteredError", "SenderIdMismatchError"} or "UNREGISTERED" in str(exc).upper():
                    invalid += 1
                    token_row.is_active = False
                    token_row.status = "invalid"
                    token_row.invalidated_at = _now()
                await self._record_delivery(row.user_id, notification_id, "mobile_push", "firebase_admin", "failed", target=f"fcm:{_token_ref(token)}", error_code=safe_error_code)
        if sent:
            return "provider_accepted"
        if invalid and not sent:
            return "failed"
        return "failed" if failed else "blocked_configuration"

    async def _send_web_push(self, row: Any, notification_id: str | None) -> str:
        settings = get_settings()
        if webpush is None:
            return await self._record_delivery(row.user_id, notification_id, "web_push", "web_push", "blocked_configuration", error_code="pywebpush_not_installed")
        if not (settings.vapid_public_key and settings.vapid_private_key and settings.vapid_subject):
            return await self._record_delivery(row.user_id, notification_id, "web_push", "web_push", "blocked_configuration", error_code="vapid_credentials_required")
        sub_model = MODEL_BY_TABLE["web_push_subscriptions"]
        subscriptions = (
            await self.session.execute(
                select(sub_model).where(
                    sub_model.user_id == row.user_id,
                    sub_model.is_active.is_(True),
                    sub_model.status == "active",
                    sub_model.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not subscriptions:
            return await self._record_delivery(row.user_id, notification_id, "web_push", "web_push", "blocked_configuration", error_code="no_active_subscriptions")
        notification = self._notification_payload(row, notification_id)
        sent = failed = 0
        for sub in subscriptions:
            endpoint = getattr(sub, "endpoint", "")
            subscription_info = {
                "endpoint": endpoint,
                "keys": {"p256dh": getattr(sub, "p256dh", ""), "auth": getattr(sub, "auth", "")},
            }
            try:
                response = webpush(
                    subscription_info=subscription_info,
                    data=json.dumps(notification, ensure_ascii=False),
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims={"sub": settings.vapid_subject},
                )
                sent += 1
                await self._record_delivery(row.user_id, notification_id, "web_push", "web_push", "provider_accepted", target=f"web:{_token_ref(endpoint)}", response_code=str(getattr(response, "status_code", "accepted")))
            except WebPushException as exc:
                failed += 1
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in {404, 410}:
                    sub.is_active = False
                    sub.status = "invalid"
                    sub.invalidated_at = _now()
                await self._record_delivery(row.user_id, notification_id, "web_push", "web_push", "failed", target=f"web:{_token_ref(endpoint)}", response_code=str(status_code or ""), error_code=exc.__class__.__name__)
        return "provider_accepted" if sent else "failed" if failed else "blocked_configuration"

    def _notification_payload(self, row: Any, notification_id: str | None) -> dict[str, Any]:
        settings = get_settings()
        payload = _safe_payload(row.payload if isinstance(row.payload, dict) else {})
        extra_data = row.extra_data if isinstance(row.extra_data, dict) else {}
        payload.setdefault("notification_id", notification_id or "")
        payload.setdefault("type", row.type or "message")
        payload.setdefault("icon", "/logo.png")
        payload.setdefault("badge", "/favicon.png")
        payload.setdefault("site_url", settings.frontend_public_url)
        if row.aggregate_id:
            payload.setdefault("entity_id", str(row.aggregate_id))
        body_value = (
            getattr(row, "message", None)
            or getattr(row, "body", None)
            or extra_data.get("body")
            or extra_data.get("message")
        )
        title = _clean_notification_text(row.title, "رفاهية التسوق")
        return {
            "title": title,
            "body": _clean_notification_text(body_value, title),
            "url": payload.get("url") or payload.get("deep_link") or "/notifications",
            "icon": payload.get("icon") or "/logo.png",
            "badge": payload.get("badge") or "/favicon.png",
            "tag": extra_data.get("dedupe_key") or notification_id or str(row.event_id),
            "data": payload,
        }

    async def _record_delivery(
        self,
        user_id: uuid.UUID,
        notification_id: str | None,
        channel: str,
        provider: str,
        status: str,
        *,
        target: str | None = None,
        response_code: str | None = None,
        error_code: str | None = None,
    ) -> str:
        settings = get_settings()
        model = MODEL_BY_TABLE["notification_delivery_attempts"]
        self.session.add(model(
            notification_id=uuid.UUID(notification_id) if notification_id and _is_uuid(notification_id) else None,
            user_id=user_id,
            channel=channel,
            target=target or f"{channel}:{user_id}",
            provider=provider,
            status=status,
            response_code=response_code,
            error_code=error_code,
            attempt_number=1,
            sent_at=_now() if status in {"sent", "provider_accepted"} else None,
            failed_at=_now() if status in {"blocked_configuration", "failed"} else None,
        ))
        return status

    def build_fcm_message(self, token: str, notification: dict[str, Any]) -> dict[str, Any]:
        return {
            "message": {
                "token": token,
                "notification": {"title": notification["title"], "body": notification.get("body") or notification.get("message") or ""},
                "data": {key: str(value) for key, value in _safe_payload(notification.get("payload") or {}).items()},
            }
        }

    def build_web_push_message(self, notification: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": notification["title"],
            "body": notification.get("body") or notification.get("message") or "",
            "url": notification.get("url") or "/notifications",
            "icon": notification.get("icon") or "/logo.png",
            "badge": notification.get("badge") or "/favicon.png",
            "tag": notification.get("deduplication_key") or notification.get("id"),
            "data": _safe_payload(notification.get("payload") or {}),
        }


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _category_from_type(notification_type: str) -> str:
    lowered = notification_type.lower()
    if "order" in lowered:
        return "order"
    if "payment" in lowered or "refund" in lowered:
        return "payment"
    if "shipping" in lowered or "delivery" in lowered:
        return "shipping"
    if "promo" in lowered or "marketing" in lowered:
        return "promotional"
    if "support" in lowered or "ticket" in lowered:
        return "support"
    if "security" in lowered or "password" in lowered or "login" in lowered:
        return "security"
    return "system"


def _category_allowed(pref: Any, category: str) -> bool:
    if category in SECURITY_CATEGORIES:
        return True
    field = {
        "order": "order_updates",
        "payment": "payment_updates",
        "shipping": "shipping_updates",
        "promotional": "promotional_notifications",
        "support": "support_updates",
        "system": "system_notifications",
    }.get(category, "system_notifications")
    return bool(getattr(pref, field, True))


def _ensure_firebase_app(settings: Any) -> str | None:
    try:
        ensure_firebase_admin_app(settings)
        return None
    except Exception as exc:
        return str(getattr(exc, "detail", None) or exc.__class__.__name__)
