from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.dialects import postgresql
from backend.app.models import MODEL_BY_TABLE
from backend.app.services import notification_service as ns


@pytest.mark.parametrize("kind", ["order_status_changed", "order_created", "shipping_update", "payment_reminder", "cart_discount", "coupon", "marketing_campaign"])
def test_customer_allowed_categories(kind):
    assert ns.customer_notification_allowed(kind)


@pytest.mark.parametrize("kind", ["password_reset_requested", "email_verification_requested", "login", "support_reply", "system", "message", "partner_application_approved", "order_invoice_ready", "payment_receipt", "unknown"])
def test_unrelated_customer_notifications_are_hidden(kind):
    assert not ns.customer_notification_allowed(kind)


class Session:
    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, ["in_app", "mobile_push", "web_push"]])
async def test_security_mirror_and_queued_notifications_cannot_bypass_policy(monkeypatch, configured):
    monkeypatch.setattr(ns, "email_delivery_configured", lambda _: True)
    monkeypatch.setattr(ns, "get_settings", lambda: SimpleNamespace())
    service = ns.NotificationService(Session())
    channels = await service._allowed_channels(uuid.uuid4(), "password_reset_requested", configured)
    assert channels == (["email"] if configured is None else [])


@pytest.mark.asyncio
async def test_email_does_not_force_marketing_push_when_disabled(monkeypatch):
    monkeypatch.setattr(ns, "email_delivery_configured", lambda _: True)
    monkeypatch.setattr(ns, "get_settings", lambda: SimpleNamespace())
    service = ns.NotificationService(Session())
    service.preferences_for = AsyncMock(return_value=SimpleNamespace(in_app_enabled=True,
        mobile_push_enabled=False, web_push_enabled=False, promotional_notifications=False))
    channels = await service._allowed_channels(uuid.uuid4(), "marketing_campaign")
    assert channels == ["in_app", "email"]


@pytest.mark.asyncio
async def test_allowed_order_status_reaches_push(monkeypatch):
    monkeypatch.setattr(ns, "email_delivery_configured", lambda _: False)
    monkeypatch.setattr(ns, "get_settings", lambda: SimpleNamespace())
    service = ns.NotificationService(Session())
    service.preferences_for = AsyncMock(return_value=SimpleNamespace(in_app_enabled=True,
        mobile_push_enabled=True, web_push_enabled=True, order_updates=True))
    assert await service._allowed_channels(uuid.uuid4(), "order_status_changed") == ["in_app", "mobile_push", "web_push"]


def test_list_and_count_policy_filters_legacy_rows_without_deleting_them():
    clause = ns.customer_notification_visible_clause(MODEL_BY_TABLE["notifications"], uuid.uuid4())
    sql = str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "user_roles" in sql
    assert "order_status_changed" in sql
    assert "password_reset_requested" not in sql
