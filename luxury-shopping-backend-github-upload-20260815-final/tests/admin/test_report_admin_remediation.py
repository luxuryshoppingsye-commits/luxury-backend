from __future__ import annotations

from datetime import date
import uuid
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import AccountSecurity, Order, OrderItem, Product, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing report/admin tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing report/admin tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if not settings.database_is_test:
        pytest.fail("Refusing report/admin tests outside a trusted test database", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing report/admin tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing report/admin tests on recovery database", pytrace=False)


async def _seed_user(role: str, run_id: str) -> tuple[User, str]:
    password = "ValidPass123"
    email = f"{run_id}-{role}-{uuid.uuid4().hex[:8]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"{role} user"))
        session.add(UserRole(user_id=user.id, role=role))
        session.add(AccountSecurity(user_id=user.id, account_status="active"))
        await session.commit()
        return user, password


async def _login(client: AsyncClient, user: User, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": user.email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _seed_paid_order(run_id: str, user_id: uuid.UUID, *, status: str = "delivered", total: Decimal = Decimal("100.00"), paid: Decimal | None = None, refund: Decimal = Decimal("0.00"), partner_id: uuid.UUID | None = None) -> uuid.UUID:
    async with SessionFactory() as session:
        product = Product(name=f"{run_id} product", sku=f"{run_id}-{uuid.uuid4().hex[:6]}", price=total, stock_quantity=5, is_active=True, approval_status="approved")
        session.add(product)
        await session.flush()
        order = Order(order_number=f"{run_id}-{uuid.uuid4().hex[:8]}", user_id=user_id, status=status, total=total, subtotal=total, payment_status="paid" if paid is not None else "pending")
        session.add(order)
        await session.flush()
        session.add(OrderItem(order_id=order.id, product_id=product.id, product_name=product.name, quantity=1, unit_price=total, total_price=total, partner_id=partner_id))
        if paid is not None:
            payment_model = MODEL_BY_TABLE["order_payments"]
            session.add(payment_model(order_id=order.id, status="approved", type="manual", amount=paid))
        if refund > 0:
            refund_model = MODEL_BY_TABLE["refunds"]
            session.add(refund_model(order_id=order.id, user_id=user_id, status="completed", amount=refund, reason="test refund"))
        await session.commit()
        return order.id


async def test_report_exports_create_real_files_and_recognize_revenue() -> None:
    _assert_safe_database()
    run_id = f"report-admin-{uuid.uuid4().hex[:8]}"
    admin, admin_password = await _seed_user("admin", run_id)
    customer, _ = await _seed_user("customer", run_id)
    await _seed_paid_order(run_id, customer.id, paid=Decimal("100.00"), refund=Decimal("30.00"))
    await _seed_paid_order(run_id, customer.id, status="cancelled", paid=Decimal("500.00"))
    await _seed_paid_order(run_id, customer.id, paid=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin, admin_password)
        source = await client.get("/api/finance/reports", headers=headers)
        csv_export = await client.post("/reports/export", headers={**headers, "Idempotency-Key": run_id}, json={"type": "orders", "format": "csv"})
        pdf_export = await client.post("/reports/export", headers=headers, json={"type": "summary", "format": "pdf"})
        csv_download = await client.get(csv_export.json()["download_url"].replace("http://testserver", ""), headers=headers)
        pdf_download = await client.get(pdf_export.json()["download_url"].replace("http://testserver", ""), headers=headers)

    assert source.status_code == 200, source.text
    revenue = source.json()["data"]["revenue"]
    assert Decimal(revenue["net_revenue"]) >= Decimal("70.00")
    assert csv_export.status_code == 200, csv_export.text
    assert csv_export.json()["status"] == "ready"
    assert csv_export.json()["ready_has_valid_file"] is True
    assert csv_download.status_code == 200
    assert b"order_number" in csv_download.content
    assert pdf_export.status_code == 200, pdf_export.text
    assert pdf_export.json()["status"] == "ready"
    assert pdf_download.status_code == 200
    assert pdf_download.content.startswith(b"%PDF")


async def test_admin_customer_permissions_and_generic_bypass_are_enforced() -> None:
    _assert_safe_database()
    run_id = f"report-admin-{uuid.uuid4().hex[:8]}"
    logistics, logistics_password = await _seed_user("logistics", run_id)
    finance, finance_password = await _seed_user("finance", run_id)
    admin, admin_password = await _seed_user("admin", run_id)
    await _seed_user("customer", run_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        logistics_headers = await _login(client, logistics, logistics_password)
        finance_headers = await _login(client, finance, finance_password)
        admin_headers = await _login(client, admin, admin_password)
        full_denied = await client.get("/admin/customers", headers=logistics_headers)
        limited_finance = await client.get("/api/admin/customers", headers=finance_headers)
        report_generic = await client.post("/resources/report_exports/query", headers=admin_headers, json={"operation": "select"})
        support_generic = await client.post("/resources/support_tickets/query", headers=admin_headers, json={"operation": "insert", "data": {"subject": "Bad", "description": "Bad"}})

    assert full_denied.status_code == 403
    assert limited_finance.status_code == 200
    assert all("roles" not in row for row in limited_finance.json()["data"])
    assert report_generic.status_code == 403
    assert support_generic.status_code == 403


async def test_campaign_scheduler_worker_metrics_and_active_endpoint() -> None:
    _assert_safe_database()
    run_id = f"report-admin-{uuid.uuid4().hex[:8]}"
    admin, admin_password = await _seed_user("admin", run_id)
    await _seed_user("customer", run_id)
    event_model = MODEL_BY_TABLE["analytics_events"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin, admin_password)
        created = await client.post("/api/marketing/campaigns", headers=headers, json={"title": f"{run_id} campaign", "message": "Campaign body", "channels": ["in_app"], "campaign_type": "promo_notification"})
        processed = await client.post("/api/marketing/campaigns/process-due", headers=headers)
        active = await client.get("/api/marketing/campaigns/active?type=promo_notification")

    assert created.status_code == 201, created.text
    assert created.json()["data"]["status"] == "queued"
    assert processed.status_code == 200
    assert processed.json()["data"]["processed"] >= 1
    async with SessionFactory() as session:
        deliveries = int((await session.execute(select(func.count()).select_from(event_model).where(event_model.type == "campaign_delivery", event_model.description == created.json()["data"]["id"]))).scalar_one())
    assert deliveries >= 1
    assert active.status_code == 200
    assert any(row["id"] == created.json()["data"]["id"] for row in active.json()["data"])


async def test_courier_location_assignment_scope_and_coordinate_validation() -> None:
    _assert_safe_database()
    run_id = f"report-admin-{uuid.uuid4().hex[:8]}"
    courier, courier_password = await _seed_user("delivery", run_id)
    other, other_password = await _seed_user("delivery", run_id)
    customer, _ = await _seed_user("customer", run_id)
    order_id = await _seed_paid_order(run_id, customer.id, paid=Decimal("100.00"))
    assignment_model = MODEL_BY_TABLE["courier_assignments"]
    async with SessionFactory() as session:
        assignment = assignment_model(user_id=courier.id, courier_id=courier.id, order_id=order_id, status="assigned")
        session.add(assignment)
        await session.commit()
        assignment_id = assignment.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        courier_headers = await _login(client, courier, courier_password)
        other_headers = await _login(client, other, other_password)
        bad_coordinate = await client.post("/delivery/location", headers=courier_headers, json={"assignmentId": str(assignment_id), "latitude": 190, "longitude": 44})
        wrong_owner = await client.post("/delivery/location", headers=other_headers, json={"assignmentId": str(assignment_id), "latitude": 15.3, "longitude": 44.2})
        valid = await client.post("/delivery/location", headers=courier_headers, json={"assignmentId": str(assignment_id), "latitude": 15.3, "longitude": 44.2, "accuracy": 5})
        generic = await client.post("/resources/courier_location_updates/query", headers=courier_headers, json={"operation": "insert", "data": {"assignment_id": str(assignment_id), "latitude": 15.3, "longitude": 44.2}})

    assert bad_coordinate.status_code == 422
    assert wrong_owner.status_code == 404
    assert valid.status_code == 200, valid.text
    assert valid.json()["assignment_id"] == str(assignment_id)
    assert generic.status_code == 403


async def test_support_workflow_and_operational_day_scope() -> None:
    _assert_safe_database()
    run_id = f"report-admin-{uuid.uuid4().hex[:8]}"
    customer, customer_password = await _seed_user("customer", run_id)
    admin, admin_password = await _seed_user("admin", run_id)
    await _seed_paid_order(run_id, customer.id, status="pending", paid=None)
    target_day = date.today().isoformat()
    # The cloned QA database may contain an operational day for today. Close
    # that fixture first so this contract tests the open/duplicate/close flow
    # deterministically without touching a real environment.
    async with SessionFactory() as session:
        day_model = MODEL_BY_TABLE["operational_days"]
        existing_day = (
            await session.execute(
                select(day_model).where(
                    day_model.deleted_at.is_(None),
                    day_model.extra_data["date"].astext == target_day,
                )
            )
        ).scalar_one_or_none()
        if existing_day is not None:
            existing_day.status = "closed"
            await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        customer_headers = await _login(client, customer, customer_password)
        admin_headers = await _login(client, admin, admin_password)
        invalid_ticket = await client.post("/api/support/tickets", headers=customer_headers, json={"subject": "طلب دعم", "description": "short"})
        valid_ticket = await client.post("/api/support/tickets", headers=customer_headers, json={"subject": "Late order question", "description": "My order is delayed and needs support.", "category": "orders", "priority": "high"})
        reply = await client.post(f"/api/support/tickets/{valid_ticket.json()['data']['id']}/messages", headers=admin_headers, json={"message": "We are checking the courier update."})
        first_open = await client.post("/api/operations/operational/days/open", headers=admin_headers, json={"date": target_day})
        opened_day = await client.get("/api/operations/operational/days/today", headers=admin_headers)
        duplicate_open = await client.post("/api/operations/operational/days/open", headers=admin_headers, json={"date": target_day})
        close_tomorrow = await client.post("/api/operations/operational/days/close", headers=admin_headers, json={"date": target_day})

    assert invalid_ticket.status_code == 422
    assert valid_ticket.status_code == 201, valid_ticket.text
    assert valid_ticket.json()["data"]["ticket_number"].startswith("SUP-")
    assert reply.status_code == 201, reply.text
    assert first_open.status_code == 200
    assert opened_day.status_code == 200
    assert opened_day.json()["data"]["date"] == target_day
    assert opened_day.json()["data"]["status"] == "open"
    assert duplicate_open.status_code == 409
    # Closing is blocked while a pending order exists for the day.
    assert close_tomorrow.status_code == 409


async def test_theme_bootstrap_sync_loyalty_and_forms_contracts() -> None:
    _assert_safe_database()
    run_id = f"report-admin-{uuid.uuid4().hex[:8]}"
    admin, admin_password = await _seed_user("admin", run_id)
    finance, finance_password = await _seed_user("finance", run_id)
    customer, customer_password = await _seed_user("customer", run_id)
    async with SessionFactory() as session:
        public_product = Product(
            name=f"{run_id} public catalog product",
            sku=f"{run_id}-pub-{uuid.uuid4().hex[:6]}",
            price=Decimal("42.00"),
            stock_quantity=2,
            is_active=True,
            approval_status="approved",
            image_url="/uploads/products/0039c8877ec3f5759d10cb9b.webp",
            images=["/uploads/products/0039c8877ec3f5759d10cb9b.webp"],
        )
        pending_product = Product(
            name=f"{run_id} pending catalog product",
            sku=f"{run_id}-pending-{uuid.uuid4().hex[:6]}",
            price=Decimal("44.00"),
            stock_quantity=2,
            is_active=True,
            approval_status="pending",
        )
        demo_tier_model = MODEL_BY_TABLE["loyalty_tiers"]
        demo_tier = demo_tier_model(
            name=f"{run_id} Demo Bronze",
            status="active",
            is_active=True,
            amount=Decimal("0"),
            sort_order=1,
            extra_data={"source": "demo"},
        )
        real_tier = demo_tier_model(
            name=f"{run_id} Real Tier",
            status="active",
            is_active=True,
            amount=Decimal("100"),
            sort_order=2,
            extra_data={"source": "admin"},
        )
        session.add_all([public_product, pending_product, demo_tier, real_tier])
        await session.commit()
        public_product_id = public_product.id
        pending_product_id = pending_product.id
        real_tier_id = real_tier.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        admin_headers = await _login(client, admin, admin_password)
        finance_headers = await _login(client, finance, finance_password)
        customer_headers = await _login(client, customer, customer_password)

        finance_theme = await client.patch(
            "/api/content/theme/default",
            headers=finance_headers,
            json={"value": {"primary": "#976817"}},
        )
        preview = await client.post(
            "/api/content/theme/preview",
            headers=admin_headers,
            json={"value": {"primary": "#111111", "buttonRadius": 8}},
        )
        preview_public = await client.get(preview.json()["preview_url"], headers=customer_headers)
        published = await client.patch(
            "/api/content/theme/default",
            headers=admin_headers,
            json={"value": {"primary": "#976817", "buttonRadius": 8}},
        )
        public_theme = await client.get("/settings/theme")
        bootstrap = await client.get("/sync/bootstrap", headers=customer_headers)
        first_status = await client.get("/sync/status?stream=orders&deviceId=device-a&platform=android", headers=customer_headers)
        pull = await client.post("/api/sync/orders/pull", headers=customer_headers, json={"deviceId": "device-a", "platform": "android", "cursor": 0})
        second_status = await client.get("/sync/status?stream=orders&deviceId=device-a&platform=android", headers=customer_headers)
        other_device_status = await client.get("/sync/status?stream=orders&deviceId=device-b&platform=web", headers=customer_headers)
        tiers = await client.get("/api/loyalty/tiers", headers=admin_headers)
        invalid_form = await client.post("/api/content/forms", headers=admin_headers, json={"settings": {"fields": "bad"}})
        form = await client.post("/api/content/forms", headers=admin_headers, json={"form_key": run_id, "settings": {"fields": [{"name": "subject"}]}})
        form_update = await client.patch(f"/api/content/forms/{run_id}", headers=admin_headers, json={"settings": {"fields": [{"name": "subject"}, {"name": "message"}]}})
        form_get = await client.get(f"/api/content/forms?key={run_id}", headers=customer_headers)

    assert finance_theme.status_code == 403
    assert preview.status_code == 200, preview.text
    assert preview_public.status_code == 200, preview_public.text
    assert published.status_code == 200, published.text
    assert public_theme.status_code == 200
    assert public_theme.json()["id"] == published.json()["data"]["id"]
    product_ids = {row["id"] for row in bootstrap.json()["products"]}
    assert str(public_product_id) in product_ids
    assert str(pending_product_id) not in product_ids
    assert first_status.status_code == 200
    assert pull.status_code == 200, pull.text
    assert second_status.json()["revision"] == pull.json()["cursor"]
    assert other_device_status.json()["revision"] == 0
    assert tiers.status_code == 200
    tier_ids = {row["id"] for row in tiers.json()["data"]}
    assert str(real_tier_id) in tier_ids
    assert all("Demo Bronze" not in row.get("name", "") for row in tiers.json()["data"])
    assert invalid_form.status_code == 422
    assert form.status_code == 201, form.text
    assert form_update.status_code == 200, form_update.text
    assert form_get.status_code == 200
    assert form_get.json()["data"]["id"] == form.json()["data"]["id"]
