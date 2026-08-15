from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..config import get_settings
from ..database import SessionFactory, engine
from ..models import MODEL_BY_TABLE
from ..services.notification_service import NotificationService
from ..services.outbox_service import process_email_outbox, process_whatsapp_outbox
from ..services.report_admin_services import CampaignService


logger = logging.getLogger("luxury.message_worker")


class MessageWorker:
    def __init__(self, *, worker_id: str | None = None, poll_seconds: float = 2.0) -> None:
        self.worker_id = worker_id or f"message-worker-{uuid.uuid4()}"
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> dict[str, Any]:
        settings = get_settings()
        async with SessionFactory() as session:
            # Publish liveness before touching any provider or outbox row. This
            # keeps the health endpoint useful even when an external provider
            # (FCM, SMTP, or WhatsApp) temporarily fails.
            await self._heartbeat(
                session,
                {
                    "notification": {},
                    "email": {},
                    "whatsapp": {},
                    "campaigns": {},
                    "cleanup": {},
                },
            )
            await session.commit()
            notification = await NotificationService(session).process_outbox_once(limit=settings.message_batch_size)
            email = await process_email_outbox(session, limit=settings.message_batch_size)
            whatsapp = await process_whatsapp_outbox(session, limit=settings.message_batch_size)
            campaigns = await CampaignService().process_due(session, limit=settings.message_batch_size)
            cleanup = await self._cleanup_unsafe_admin_notifications(session)
            await self._heartbeat(session, {"notification": notification, "email": email, "whatsapp": whatsapp, "campaigns": campaigns, "cleanup": cleanup})
            await session.commit()
            return {"notification": notification, "email": email, "whatsapp": whatsapp, "campaigns": campaigns, "cleanup": cleanup}

    async def run_forever(self) -> None:
        logger.info("message worker started: %s", self.worker_id)
        while not self._stop.is_set():
            try:
                result = await self.run_once()
                processed = sum(int((result.get(channel) or {}).get("claimed") or (result.get(channel) or {}).get("total") or 0) for channel in result)
                if processed:
                    logger.info("message worker processed batch")
            except Exception as exc:
                logger.warning("message worker batch failed: %s", exc.__class__.__name__)
                await self._record_failure(exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
        await engine.dispose()
        logger.info("message worker stopped: %s", self.worker_id)

    async def _record_failure(self, exc: Exception) -> None:
        """Keep a safe provider-independent failure marker for diagnostics."""
        try:
            async with SessionFactory() as session:
                model = MODEL_BY_TABLE["operational_alerts"]
                existing = (
                    await session.execute(
                        select(model)
                        .where(model.type == "message_worker_heartbeat", model.status == "active", model.deleted_at.is_(None))
                        .order_by(model.updated_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    detail = " ".join(str(exc).split())[:160]
                    if any(word in detail.lower() for word in ("token", "password", "secret", "credential")):
                        detail = ""
                    existing.extra_data = {
                        **(existing.extra_data or {}),
                        "last_error_type": exc.__class__.__name__,
                        "last_error_at": datetime.now(timezone.utc).isoformat(),
                        "last_error_detail": detail,
                    }
                    await session.commit()
        except Exception as diagnostic_error:
            logger.warning("message worker failure marker failed: %s", diagnostic_error.__class__.__name__)

    async def _heartbeat(self, session: Any, payload: dict[str, Any]) -> None:
        model = MODEL_BY_TABLE["operational_alerts"]
        existing = (
            await session.execute(
                select(model)
                .where(model.type == "message_worker_heartbeat", model.status == "active", model.deleted_at.is_(None))
                .order_by(model.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        safe_payload = {
            "worker_id": self.worker_id,
            "notification_total": int(payload["notification"].get("total", 0)),
            "email_claimed": int(payload["email"].get("claimed", 0)),
            "whatsapp_claimed": int(payload["whatsapp"].get("claimed", 0)),
            "campaigns_processed": int(payload["campaigns"].get("processed", 0)),
            "campaigns_sent": int(payload["campaigns"].get("sent", 0)),
            "campaigns_blocked_credentials": int(payload["campaigns"].get("blocked_credentials", 0)),
            "unsafe_admin_notifications_soft_deleted": int(payload["cleanup"].get("unsafe_admin_notifications_soft_deleted", 0)),
        }
        if existing is None:
            session.add(model(type="message_worker_heartbeat", status="active", description="Messaging worker heartbeat", extra_data=safe_payload))
        else:
            existing.description = "Messaging worker heartbeat"
            existing.extra_data = {**(existing.extra_data or {}), **safe_payload}

    async def _cleanup_unsafe_admin_notifications(self, session: Any) -> dict[str, int]:
        model = MODEL_BY_TABLE["admin_notifications"]
        rows = (
            await session.execute(
                select(model)
                .where(
                    model.deleted_at.is_(None),
                    model.recipient_id.is_(None),
                    model.user_id.is_(None),
                )
                .with_for_update(skip_locked=True)
                .limit(500)
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            row.deleted_at = now
            row.status = "quarantined"
            row.extra_data = {
                **(row.extra_data or {}),
                "cleanup_reason": "missing_admin_notification_recipient",
                "cleaned_by": self.worker_id,
            }
        return {"unsafe_admin_notifications_soft_deleted": len(rows)}


async def _main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    worker = MessageWorker(worker_id=os.getenv("MESSAGE_WORKER_ID"))
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            pass
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
