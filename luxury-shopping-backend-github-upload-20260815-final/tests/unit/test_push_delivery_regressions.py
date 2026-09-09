from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from backend.app.services import notification_service as ns
from backend.app.services import report_admin_services as rs


class Session:
    def __init__(self):
        self.added = []

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []))

    def add(self, row):
        self.added.append(row)


def outbox():
    return SimpleNamespace(user_id=uuid.uuid4(), type="customer_welcome",
        id=uuid.uuid4(), aggregate_id=None, event_id=uuid.uuid4(), payload={}, extra_data={}, title="Welcome", message="Welcome", body="Welcome")


@pytest.mark.asyncio
async def test_email_success_cannot_hide_push_configuration_failure():
    service = ns.NotificationService(Session())
    service._allowed_channels = AsyncMock(return_value=["email", "mobile_push"])
    service._deliver_channel = AsyncMock(side_effect=["sent", "blocked_configuration"])
    row = outbox()
    result = await service._deliver_outbox(row)
    assert not result["ok"] and result["blocked"]
    assert result["error"] == "mobile_push:blocked_configuration"
    assert row.extra_data["delivered_channels"] == {"email": "sent"}
    service._deliver_channel = AsyncMock(return_value="provider_accepted")
    assert (await service._deliver_outbox(row))["ok"]
    service._deliver_channel.assert_awaited_once_with(row, None, "mobile_push")


@pytest.mark.asyncio
async def test_waiting_for_device_retries_even_if_web_push_is_not_configured():
    service = ns.NotificationService(Session())
    service._allowed_channels = AsyncMock(return_value=["in_app", "mobile_push", "web_push"])
    service._deliver_channel = AsyncMock(side_effect=["sent", "failed_retryable", "blocked_configuration"])
    result = await service._deliver_outbox(outbox())
    assert not result["ok"] and not result["blocked"]
    assert "mobile_push:failed_retryable" in result["error"]


@pytest.mark.asyncio
async def test_welcome_waits_for_device_registration(monkeypatch):
    monkeypatch.setattr(ns, "get_settings", lambda: SimpleNamespace(firebase_project_id="test",
        google_application_credentials=None, google_application_credentials_json="{}",
        firebase_service_account_json=None))
    monkeypatch.setattr(ns, "_ensure_firebase_app", lambda _: None)
    service = ns.NotificationService(Session())
    service._record_delivery = AsyncMock(return_value="failed_retryable")
    assert await service._send_mobile_push(outbox(), None) == "failed_retryable"
    assert service._record_delivery.await_args.kwargs["error_code"] == "no_active_tokens"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,enabled,expected", [
    ("push", True, ("mobile_push",)), ("in_app", True, ("in_app",)),
    ("push", False, ("mobile_push",)),
])
async def test_production_campaign_push_is_queued_for_every_target(monkeypatch, channel, enabled, expected):
    monkeypatch.setattr(rs, "get_settings", lambda: SimpleNamespace(app_env="production"))
    notification = SimpleNamespace(
        preferences_for=AsyncMock(return_value=SimpleNamespace(promotional_notifications=enabled,
            mobile_push_enabled=True, in_app_enabled=True)),
        create_notification=AsyncMock())
    monkeypatch.setattr(rs, "NotificationService", lambda _: notification)
    session = Session()
    row = SimpleNamespace(id=uuid.uuid4(), title="Friday", message="Have a good Friday", created_by=uuid.uuid4())
    result = await rs.CampaignService()._deliver_batch(session, row, [uuid.uuid4()], [channel])
    assert result["blocked_credentials"] == 0
    payload = notification.create_notification.await_args.args[0]
    assert payload.delivery_channels == expected
    assert payload.notification_type == "marketing_campaign"
    assert result["sent"] == 1


def test_campaigns_always_include_in_app_and_phone_alert_channels():
    normalized = rs.CampaignService()._normalize_body({
        "title": "إعلان جديد",
        "message": "وصلت منتجات وعروض جديدة إلى المتجر.",
        "channel": "email",
    })
    assert {"in_app", "push"}.issubset(normalized["channels"])


@pytest.mark.asyncio
async def test_mobile_payload_has_visible_alert_sound_and_android_channel(monkeypatch):
    settings = SimpleNamespace(firebase_project_id="test", google_application_credentials=None,
        google_application_credentials_json="{}", firebase_service_account_json=None,
        frontend_public_url="https://example.test")
    monkeypatch.setattr(ns, "get_settings", lambda: settings)
    monkeypatch.setattr(ns, "_ensure_firebase_app", lambda _: None)
    captured = []
    monkeypatch.setattr(ns.messaging, "send", lambda message: captured.append(message) or "test-message-id")
    session = Session()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda:
        SimpleNamespace(all=lambda: [SimpleNamespace(token="test-only-device-token")])) )
    service = ns.NotificationService(session)
    service._record_delivery = AsyncMock(return_value="provider_accepted")
    assert await service._send_mobile_push(outbox(), None) == "provider_accepted"
    message = captured[0]
    assert message.notification.title == "Welcome"
    assert message.notification.body == "Welcome"
    assert message.apns.headers == {"apns-priority": "10", "apns-push-type": "alert"}
    assert message.apns.payload.aps.sound == "default"
    assert message.android.priority == "high"
    assert message.android.notification.channel_id == "luxury_notifications"
    assert message.data["notification_type"] == "customer_welcome"
