from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, WebSocket
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import User
from ..repositories.resources import serialize_record


REALTIME_PROTOCOL = "luxury.realtime.v1"
TICKET_PROTOCOL_PREFIX = "rt."
ADMIN_INVENTORY_ROLES = frozenset({"admin", "manager"})
COURIER_ROLES = frozenset({"courier", "delivery"})
MERCHANT_ROLES = frozenset({"partner"})
ALLOWED_INBOUND_TYPES = frozenset({"pong", "subscribe", "unsubscribe", "resume", "ack"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_device(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:32] if value.strip() else ""


def _origin_base(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().rstrip("/")


@dataclass(frozen=True)
class RealtimeTicket:
    token: str
    expires_at: datetime
    channels: tuple[str, ...]
    websocket_url: str


@dataclass(frozen=True)
class RealtimeSession:
    ticket_id: uuid.UUID
    user_id: uuid.UUID
    roles: frozenset[str]
    channels: frozenset[str]
    device_hash: str
    platform: str
    issued_origin: str
    last_event_id: str | None


@dataclass
class HubConnection:
    websocket: WebSocket
    connection_id: str
    user_id: str
    ip: str
    channels: set[str]
    outbound_queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=200))
    inbound_timestamps: deque[float] = field(default_factory=deque)


class RealtimePolicy:
    @staticmethod
    def validate_origin(origin: str | None, *, platform: str) -> str:
        settings = get_settings()
        normalized = _origin_base(origin)
        if not normalized:
            if platform in {"android", "ios", "flutter", "mobile"}:
                return ""
            raise HTTPException(status_code=403, detail="websocket_origin_required")
        allowed = settings.allowed_realtime_origins
        if normalized in allowed:
            return normalized
        if settings.app_env == "test" and re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", normalized):
            return normalized
        raise HTTPException(status_code=403, detail="websocket_origin_denied")

    @staticmethod
    def authorize_channels(user: User, roles: set[str], requested: list[str]) -> tuple[str, ...]:
        if not requested:
            requested = ["notifications"]
        canonical: list[str] = []
        for raw_channel in requested:
            channel = str(raw_channel or "").strip()
            if not channel:
                continue
            resolved = RealtimePolicy._authorize_channel(user, roles, channel)
            if resolved not in canonical:
                canonical.append(resolved)
        if not canonical:
            raise HTTPException(status_code=422, detail="no_realtime_channels")
        return tuple(canonical)

    @staticmethod
    def _authorize_channel(user: User, roles: set[str], channel: str) -> str:
        user_channel = f"user:{user.id}"
        courier_channel = f"courier:{user.id}"
        merchant_inventory_channel = f"inventory:partner:{user.id}"
        if channel in {"notifications", user_channel}:
            return user_channel
        if channel in {"courier", courier_channel} and roles.intersection(COURIER_ROLES):
            return courier_channel
        if channel == "inventory" and roles.intersection(ADMIN_INVENTORY_ROLES):
            return "inventory"
        if channel in {"merchant_inventory", merchant_inventory_channel} and roles.intersection(MERCHANT_ROLES):
            return merchant_inventory_channel
        raise HTTPException(status_code=403, detail="realtime_channel_denied")


class RealtimeTicketService:
    async def issue(
        self,
        session: AsyncSession,
        *,
        user: User,
        roles: set[str],
        requested_channels: list[str],
        device_id: str,
        platform: str,
        origin: str | None,
        last_event_id: str | None = None,
    ) -> RealtimeTicket:
        settings = get_settings()
        normalized_platform = str(platform or "web").strip().lower()
        normalized_origin = RealtimePolicy.validate_origin(origin, platform=normalized_platform)
        channels = RealtimePolicy.authorize_channels(user, roles, requested_channels)
        raw = secrets.token_urlsafe(32)
        digest = _hash_secret(raw)
        expires_at = _now() + timedelta(seconds=settings.realtime_ticket_ttl_seconds)
        model = MODEL_BY_TABLE["sync_events"]
        row = model(
            user_id=user.id,
            type="realtime_ticket",
            status="issued",
            description=digest,
            extra_data={
                "channels": list(channels),
                "device_hash": _hash_device(device_id),
                "platform": normalized_platform,
                "origin": normalized_origin,
                "expires_at": expires_at.isoformat(),
                "last_event_id": last_event_id,
                "single_use": True,
            },
        )
        session.add(row)
        await session.flush()
        return RealtimeTicket(
            token=raw,
            expires_at=expires_at,
            channels=channels,
            websocket_url="/ws/realtime",
        )

    async def consume(self, session: AsyncSession, *, ticket: str, origin: str | None) -> RealtimeSession:
        digest = _hash_secret(ticket)
        row = (
            await session.execute(
                text(
                    """
                    select id, user_id, status, extra_data
                    from sync_events
                    where deleted_at is null and type = 'realtime_ticket' and description = :digest
                    order by created_at desc
                    limit 1
                    for update
                    """
                ),
                {"digest": digest},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=401, detail="realtime_ticket_invalid")
        extra = row["extra_data"] or {}
        expires_at = datetime.fromisoformat(str(extra.get("expires_at")))
        if expires_at <= _now():
            await session.execute(text("update sync_events set status='expired', updated_at=now() where id=:id"), {"id": row["id"]})
            raise HTTPException(status_code=401, detail="realtime_ticket_expired")
        if row["status"] != "issued":
            raise HTTPException(status_code=401, detail="realtime_ticket_used")
        platform = str(extra.get("platform") or "web").lower()
        current_origin = RealtimePolicy.validate_origin(origin, platform=platform)
        issued_origin = str(extra.get("origin") or "")
        if platform not in {"android", "ios", "flutter", "mobile"} and issued_origin != current_origin:
            raise HTTPException(status_code=403, detail="realtime_ticket_origin_mismatch")
        if platform in {"android", "ios", "flutter", "mobile"} and issued_origin and issued_origin != current_origin:
            raise HTTPException(status_code=403, detail="realtime_ticket_origin_mismatch")
        user = await session.get(User, row["user_id"])
        if user is None or not user.is_active or user.deleted_at is not None:
            raise HTTPException(status_code=401, detail="inactive_user")
        roles = frozenset((await session.execute(text("select role from user_roles where user_id=:user_id"), {"user_id": user.id})).scalars())
        await session.execute(
            text(
                """
                update sync_events
                set status='used',
                    extra_data = coalesce(extra_data, '{}'::jsonb) || jsonb_build_object('used_at', now())
                where id=:id
                """
            ),
            {"id": row["id"]},
        )
        return RealtimeSession(
            ticket_id=row["id"],
            user_id=user.id,
            roles=roles,
            channels=frozenset(str(item) for item in extra.get("channels", [])),
            device_hash=str(extra.get("device_hash") or ""),
            platform=platform,
            issued_origin=str(extra.get("origin") or ""),
            last_event_id=str(extra.get("last_event_id") or "") or None,
        )


class RealtimeEventService:
    async def record_event(
        self,
        session: AsyncSession,
        *,
        channel: str,
        event: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        model = MODEL_BY_TABLE["sync_events"]
        event_id = str(uuid.uuid4())
        resolved_dedupe = dedupe_key or f"{channel}:{event}:{hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()}"
        existing = (
            await session.execute(
                select(model).where(
                    model.type == f"realtime_event:{channel}",
                    model.description == resolved_dedupe,
                    model.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existing is not None:
            return serialize_record(existing)
        row = model(
            user_id=user_id,
            type=f"realtime_event:{channel}",
            status="pending",
            description=resolved_dedupe,
            extra_data={
                "event_id": event_id,
                "event": event,
                "channel": channel,
                "payload": payload,
                "published_at": None,
                "dedupe_key": resolved_dedupe,
            },
        )
        session.add(row)
        await session.flush()
        return serialize_record(row)

    @staticmethod
    def event_payload_from_record(row: Any) -> dict[str, Any]:
        extra = row["extra_data"] if isinstance(row, Mapping) else row.extra_data
        created_at = row["created_at"] if isinstance(row, Mapping) else row.created_at
        row_id = row["id"] if isinstance(row, Mapping) else row.id
        extra = extra or {}
        return {
            "type": str(extra.get("event") or "event"),
            "event": str(extra.get("event") or "event"),
            "event_id": str(extra.get("event_id") or row_id),
            "channel": extra.get("channel"),
            "payload": extra.get("payload") or {},
            "created_at": created_at.isoformat() if created_at else None,
        }

    async def replay(self, session: AsyncSession, *, channels: set[str], after_event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not channels:
            return []
        rows = (
            await session.execute(
                text(
                    """
                    select id::text, created_at, extra_data
                    from sync_events
                    where deleted_at is null
                      and type = any(:types)
                      and status in ('pending', 'published')
                    order by created_at asc
                    limit :limit
                    """
                ),
                {"types": [f"realtime_event:{channel}" for channel in channels], "limit": limit},
            )
        ).mappings().all()
        events: list[dict[str, Any]] = []
        seen_after = after_event_id is None
        for row in rows:
            extra = row["extra_data"] or {}
            event_id = str(extra.get("event_id") or row["id"])
            if not seen_after:
                if event_id == after_event_id:
                    seen_after = True
                continue
            payload = self.event_payload_from_record(row)
            payload["replayed"] = True
            events.append(payload)
        return events

    async def mark_published(self, session: AsyncSession, event_id: str) -> None:
        await session.execute(
            text(
                """
                update sync_events
                set status='published',
                    extra_data = coalesce(extra_data, '{}'::jsonb) || jsonb_build_object('published_at', now())
                where type like 'realtime_event:%' and extra_data->>'event_id' = :event_id
                """
            ),
            {"event_id": event_id},
        )


class RealtimeHub:
    def __init__(self) -> None:
        self._connections: dict[str, dict[str, HubConnection]] = defaultdict(dict)
        self._by_user: dict[str, set[str]] = defaultdict(set)
        self._by_ip: dict[str, set[str]] = defaultdict(set)
        self._seen_event_ids: deque[str] = deque(maxlen=5000)
        self._seen_event_id_set: set[str] = set()
        self._fanout_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def connect(self, channel: str, websocket: WebSocket) -> None:
        await websocket.accept()
        connection = HubConnection(
            websocket=websocket,
            connection_id=uuid.uuid4().hex,
            user_id="legacy",
            ip=websocket.client.host if websocket.client else "unknown",
            channels={channel},
        )
        async with self._lock:
            self._connections[channel][connection.connection_id] = connection

    async def register(self, realtime_session: RealtimeSession, websocket: WebSocket) -> HubConnection:
        settings = get_settings()
        ip = websocket.client.host if websocket.client else "unknown"
        connection_id = uuid.uuid4().hex
        async with self._lock:
            self._ensure_fanout_locked()
            if len(self._by_user[str(realtime_session.user_id)]) >= settings.realtime_max_connections_per_user:
                raise HTTPException(status_code=429, detail="realtime_user_connection_limit")
            if len(self._by_ip[ip]) >= settings.realtime_max_connections_per_ip:
                raise HTTPException(status_code=429, detail="realtime_ip_connection_limit")
            connection = HubConnection(
                websocket=websocket,
                connection_id=connection_id,
                user_id=str(realtime_session.user_id),
                ip=ip,
                channels=set(realtime_session.channels),
            )
            for channel in connection.channels:
                self._connections[channel][connection_id] = connection
            self._by_user[connection.user_id].add(connection_id)
            self._by_ip[ip].add(connection_id)
            return connection

    def _ensure_fanout_locked(self) -> None:
        if self._fanout_task is None or self._fanout_task.done():
            self._fanout_task = asyncio.create_task(self._fanout_loop())

    def _remember_event(self, event_id: str) -> bool:
        if not event_id:
            return True
        if event_id in self._seen_event_id_set:
            return False
        if len(self._seen_event_ids) == self._seen_event_ids.maxlen:
            old = self._seen_event_ids.popleft()
            self._seen_event_id_set.discard(old)
        self._seen_event_ids.append(event_id)
        self._seen_event_id_set.add(event_id)
        return True

    async def disconnect(self, channel: str, websocket: WebSocket) -> None:
        async with self._lock:
            for connections in self._connections.values():
                for connection_id, connection in list(connections.items()):
                    if connection.websocket is websocket:
                        connections.pop(connection_id, None)
                        self._by_user[connection.user_id].discard(connection_id)
                        self._by_ip[connection.ip].discard(connection_id)
            self._connections[channel].pop(str(id(websocket)), None)

    async def disconnect_connection(self, connection: HubConnection) -> None:
        async with self._lock:
            for channel in connection.channels:
                self._connections[channel].pop(connection.connection_id, None)
            self._by_user[connection.user_id].discard(connection.connection_id)
            self._by_ip[connection.ip].discard(connection.connection_id)

    async def subscribe_connection(self, connection: HubConnection, channel: str) -> None:
        async with self._lock:
            connection.channels.add(channel)
            self._connections[channel][connection.connection_id] = connection

    async def unsubscribe_connection(self, connection: HubConnection, channel: str) -> None:
        async with self._lock:
            connection.channels.discard(channel)
            self._connections[channel].pop(connection.connection_id, None)

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        dead: list[HubConnection] = []
        for connection in list(self._connections.get(channel, {}).values()):
            try:
                message = json.dumps(event, ensure_ascii=False, default=str)
                if connection.outbound_queue.full():
                    dead.append(connection)
                    continue
                await connection.outbound_queue.put(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            await self.disconnect_connection(connection)

    async def publish_recorded_event(self, channel: str, event: dict[str, Any]) -> None:
        if not self._remember_event(str(event.get("event_id") or "")):
            return
        await self.publish(channel, event)

    async def _fanout_loop(self) -> None:
        from ..database import SessionFactory

        while True:
            try:
                settings = get_settings()
                cutoff = _now() - timedelta(seconds=settings.realtime_event_retention_seconds)
                async with SessionFactory() as session:
                    rows = (
                        await session.execute(
                            text(
                                """
                                select id::text, created_at, status, extra_data
                                from sync_events
                                where deleted_at is null
                                  and type like 'realtime_event:%'
                                  and status in ('pending', 'published')
                                  and created_at >= :cutoff
                                order by created_at asc
                                limit 200
                                """
                            ),
                            {"cutoff": cutoff},
                        )
                    ).mappings().all()
                    for row in rows:
                        event = RealtimeEventService.event_payload_from_record(row)
                        channel = str(event.get("channel") or "")
                        if not channel:
                            continue
                        if not self._remember_event(str(event.get("event_id") or "")):
                            continue
                        await self.publish(channel, event)
                        if row["status"] == "pending":
                            await RealtimeEventService().mark_published(session, str(event.get("event_id") or ""))
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2)
                continue
            await asyncio.sleep(1)

    async def shutdown(self) -> None:
        task = self._fanout_task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._fanout_task = None

    async def send_loop(self, connection: HubConnection) -> None:
        while True:
            message = await connection.outbound_queue.get()
            await connection.websocket.send_text(message)


def extract_realtime_ticket(websocket: WebSocket) -> str | None:
    protocols = websocket.headers.get("sec-websocket-protocol") or ""
    for item in [part.strip() for part in protocols.split(",")]:
        if item.startswith(TICKET_PROTOCOL_PREFIX):
            return item.removeprefix(TICKET_PROTOCOL_PREFIX)
    return None


async def receive_secure_message(connection: HubConnection) -> dict[str, Any] | None:
    settings = get_settings()
    raw = await connection.websocket.receive_text()
    if len(raw.encode("utf-8")) > settings.realtime_max_message_bytes:
        await connection.websocket.close(code=1008)
        return None
    now = time.monotonic()
    connection.inbound_timestamps.append(now)
    while connection.inbound_timestamps and connection.inbound_timestamps[0] < now - 60:
        connection.inbound_timestamps.popleft()
    if len(connection.inbound_timestamps) > settings.realtime_max_inbound_messages_per_minute:
        await connection.websocket.close(code=1008)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        await connection.websocket.close(code=1003)
        return None
    message_type = str(payload.get("type") or "")
    if message_type not in ALLOWED_INBOUND_TYPES:
        await connection.websocket.close(code=1008)
        return None
    return payload


realtime_hub = RealtimeHub()
