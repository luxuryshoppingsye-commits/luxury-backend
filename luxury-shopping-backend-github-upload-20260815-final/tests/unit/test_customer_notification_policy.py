from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.dialects import postgresql
from backend.app.models import MODEL_BY_TABLE
from backend.app.api.routes import operations as operations_routes
from backend.app.repositories import resources as resource_repository
from backend.app.services import notification_service as ns
from backend.app.services import outbox_service


@pytest.mark.parametrize("kind", ["order_status_changed", "order_created", "shipping_update", "payment_reminder", "payment_status_changed", "cart_discount", "coupon", "marketing_campaign", "cart_reminder", "customer_welcome", "welcome_popup", "customer_message_staff", "admin_direct_message", "admin_broadcast_message", "admin_broadcast", "admin_message", "general_alert", "promo_notification", "ticket_opened", "support_reply", "partner_application_approved", "partner_application_rejected", "partner_approved", "partner_rejected", "storefront_approved", "storefront_rejected", "product_submitted_for_review", "product_approved", "product_rejected", "store_review_approved", "friday_greeting", "email_message", "email_verification_requested", "order_invoice_ready", "order_confirmation_resent", "payment_receipt", "message", "info"])
def test_customer_allowed_categories(kind):
    assert ns.customer_notification_allowed(kind)


@pytest.mark.asyncio
async def test_payment_status_notification_has_customer_scope_and_english_copy(monkeypatch):
    notifications = SimpleNamespace(create_notification=AsyncMock())
    monkeypatch.setattr(ns, "NotificationService", lambda _: notifications)
    user_id = uuid.uuid4()
    await ns.create_payment_status_notification(
        Session(),
        user_id=user_id,
        status="paid",
        international=True,
        entity_type="international_orders",
        entity_id=str(uuid.uuid4()),
        action_url="/international-orders/1",
        created_by=uuid.uuid4(),
    )
    payload = notifications.create_notification.await_args.args[0]
    assert payload.user_id == user_id
    assert payload.notification_type == "payment_status_changed"
    assert payload.category == "payment"
    assert payload.payload["title_en"] == "Payment status updated for your international order"
    assert payload.payload["payment_status"] == "paid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_data", "record", "expected_type"),
    [
        (
            {"status": "reviewing"},
            SimpleNamespace(
                id=uuid.uuid4(), user_id=uuid.uuid4(), status="reviewing", amount=100,
                extra_data={},
            ),
            "order_status",
        ),
        (
            {"final_price": 500},
            SimpleNamespace(
                id=uuid.uuid4(), user_id=uuid.uuid4(), status="pending", amount=100,
                extra_data={"final_price": 500},
            ),
            "order_update",
        ),
    ],
)
async def test_local_request_updates_create_customer_notifications(
    monkeypatch, changed_data, record, expected_type
):
    notifications = SimpleNamespace(create_notification=AsyncMock())
    monkeypatch.setattr(ns, "NotificationService", lambda _: notifications)
    previous = {"status": "pending", "amount": 100, "extra_data": {}}
    await resource_repository._notify_customer_resource_update(
        Session(),
        "local_shopping_requests",
        record,
        previous,
        changed_data,
        uuid.uuid4(),
    )
    payload = notifications.create_notification.await_args.args[0]
    assert payload.user_id == record.user_id
    assert payload.notification_type == expected_type
    assert payload.action_url == "/local-shopping"


@pytest.mark.asyncio
async def test_international_status_update_creates_one_customer_notification(monkeypatch):
    notifications = SimpleNamespace(create_notification=AsyncMock())
    monkeypatch.setattr(operations_routes, "NotificationService", lambda _: notifications)
    order_id = uuid.uuid4()
    user_id = uuid.uuid4()
    order = SimpleNamespace(id=order_id, user_id=user_id)

    await operations_routes._create_international_order_status_notification(
        Session(),
        order=order,
        previous_status="pending",
        next_status="confirmed",
        created_by=uuid.uuid4(),
    )

    notifications.create_notification.assert_awaited_once()
    payload = notifications.create_notification.await_args.args[0]
    assert payload.user_id == user_id
    assert payload.notification_type == "order_status"
    assert payload.category == "order"
    assert payload.action_url == f"/international-orders/{order_id}"
    assert payload.payload["order_status"] == "confirmed"
    assert payload.deduplication_key == f"international-order-status:{order_id}:pending:confirmed"


@pytest.mark.asyncio
async def test_payment_record_update_resolves_parent_customer(monkeypatch):
    payment_id = uuid.uuid4()
    order_id = uuid.uuid4()
    user_id = uuid.uuid4()
    payment = SimpleNamespace(id=payment_id, order_id=order_id, status="paid", extra_data={})
    parent = SimpleNamespace(id=order_id, user_id=user_id)
    session = SimpleNamespace(get=AsyncMock(return_value=parent))
    payment_notification = AsyncMock()
    monkeypatch.setattr(ns, "create_payment_status_notification", payment_notification)
    await resource_repository._notify_customer_resource_update(
        session,
        "order_payments",
        payment,
        {"status": "pending", "extra_data": {}},
        {"status": "paid"},
        uuid.uuid4(),
    )
    payment_notification.assert_awaited_once()
    assert payment_notification.await_args.kwargs["user_id"] == user_id
    assert payment_notification.await_args.kwargs["entity_type"] == "orders"


@pytest.mark.asyncio
async def test_merchant_rejection_can_reach_customer_app_and_push(monkeypatch):
    monkeypatch.setattr(ns, "email_delivery_configured", lambda _: False)
    monkeypatch.setattr(ns, "get_settings", lambda: SimpleNamespace())
    service = ns.NotificationService(Session())
    service.preferences_for = AsyncMock(return_value=SimpleNamespace(
        in_app_enabled=True, mobile_push_enabled=True, web_push_enabled=True,
        system_notifications=True,
    ))
    assert await service._allowed_channels(uuid.uuid4(), "partner_application_rejected") == [
        "in_app", "mobile_push", "web_push",
    ]


@pytest.mark.parametrize("kind", ["password_reset_requested", "login", "system", "unknown"])
def test_unrelated_customer_notifications_are_hidden(kind):
    assert not ns.customer_notification_allowed(kind)


def test_email_popup_mirror_skips_password_reset_and_existing_notification_rows():
    assert outbox_service._email_popup_mirror_skipped(SimpleNamespace(
        title="استعادة كلمة المرور",
        extra_data={"purpose": "password_reset"},
    ))
    assert outbox_service._email_popup_mirror_skipped(SimpleNamespace(
        title="فاتورتك",
        extra_data={"notification_id": "already-created"},
    ))
    assert not outbox_service._email_popup_mirror_skipped(SimpleNamespace(
        title="فاتورتك",
        extra_data={},
    ))


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
async def test_customer_notifications_force_mobile_push_when_disabled(monkeypatch):
    monkeypatch.setattr(ns, "email_delivery_configured", lambda _: True)
    monkeypatch.setattr(ns, "get_settings", lambda: SimpleNamespace())
    service = ns.NotificationService(Session())
    service.preferences_for = AsyncMock(return_value=SimpleNamespace(in_app_enabled=True,
        mobile_push_enabled=False, web_push_enabled=False, promotional_notifications=False))
    channels = await service._allowed_channels(uuid.uuid4(), "marketing_campaign")
    assert channels == ["in_app", "email", "mobile_push"]


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
