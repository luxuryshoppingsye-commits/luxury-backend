from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import AccountSecurity, FileAsset, Order, Profile, User, UserRole
from backend.app.security.passwords import hash_password
from backend.app.services.report_admin_services import ThemeAdminService
from backend.app.services.secure_backup import BackupCoordinator, FileSnapshotService


def _assert_isolated_test_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    assert settings.app_env == "test"
    assert settings.allow_test_fixtures is True
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port == 55433
    assert settings.database_is_test is True
    assert settings.database_name != "luxury_official_recovery"


async def _seed_admin(suffix: str) -> tuple[User, str]:
    password = "ValidPass123"
    email = f"four-defects-admin-{suffix}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name="مدير اختبار الأعطال"))
        session.add(UserRole(user_id=user.id, role="admin"))
        session.add(AccountSecurity(user_id=user.id, account_status="active"))
        await session.commit()
        return user, password


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _delete_users(user_ids: list[uuid.UUID]) -> None:
    async with SessionFactory() as session:
        for model in (UserRole, AccountSecurity, Profile, User):
            column = model.user_id if model is not User else model.id
            await session.execute(delete(model).where(column.in_(user_ids)))
        await session.commit()


@pytest.mark.asyncio
async def test_partner_delete_is_persistent_after_admin_list_reload() -> None:
    _assert_isolated_test_database()
    suffix = uuid.uuid4().hex[:12]
    admin, password = await _seed_admin(suffix)
    partner = User(email=f"four-defects-partner-{suffix}@example.com", password_hash=hash_password("Partner123"), is_active=True)
    storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    contract_model = MODEL_BY_TABLE["partner_contracts"]
    async with SessionFactory() as session:
        session.add(partner)
        await session.flush()
        session.add(Profile(id=partner.id, user_id=partner.id, email=partner.email, full_name="تاجر اختبار الحذف"))
        session.add(UserRole(user_id=partner.id, role="partner"))
        session.add(AccountSecurity(user_id=partner.id, account_status="active"))
        storefront = storefront_model(
            user_id=partner.id,
            partner_id=partner.id,
            name=f"تاجر اختبار {suffix}",
            email=partner.email,
            status="active",
            is_active=True,
        )
        contract = contract_model(partner_id=partner.id, status="active", is_active=True, extra_data={})
        session.add_all([storefront, contract])
        await session.commit()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _login(client, admin.email, password)
            before = await client.get("/api/partnership/partners", headers=headers)
            assert before.status_code == 200
            assert any(row["id"] == str(partner.id) for row in before.json()["data"])

            removed = await client.request(
                "DELETE",
                f"/api/partnership/partners/{partner.id}",
                headers=headers,
                json={"deactivateProducts": False},
            )
            assert removed.status_code == 200, removed.text
            assert removed.json()["removed_storefronts"] == 1
            assert removed.json()["removed_contracts"] == 1

            after = await client.get("/api/partnership/partners", headers=headers)
            assert after.status_code == 200
            assert all(row["id"] != str(partner.id) for row in after.json()["data"])
    finally:
        async with SessionFactory() as session:
            await session.execute(delete(storefront_model).where(storefront_model.partner_id == partner.id))
            await session.execute(delete(contract_model).where(contract_model.partner_id == partner.id))
            await session.commit()
        await _delete_users([admin.id, partner.id])


@pytest.mark.asyncio
async def test_backup_records_missing_asset_without_returning_file_missing(tmp_path: Path) -> None:
    _assert_isolated_test_database()
    asset_id = uuid.uuid4()
    asset = FileAsset(
        id=asset_id,
        policy_key="public_image",
        visibility="public",
        storage_provider="local_uploads",
        storage_bucket="uploads",
        storage_key=f"qa-missing/{asset_id}.webp",
        original_filename="missing.webp",
        content_type="image/webp",
        size_bytes=1,
        checksum_sha256="0" * 64,
        status="available",
        scan_status="clean",
    )
    async with SessionFactory() as session:
        session.add(asset)
        await session.commit()
        try:
            result = await FileSnapshotService(upload_root=tmp_path / "uploads").collect(session, tmp_path / "bundle")
            missing = result["missing_files"]
            assert result["complete"] is False
            assert result["missing_file_count"] == len(missing)
            asset_missing = next(item for item in missing if item["file_id"] == str(asset_id))
            assert asset_missing["reason"] == "local_file_missing"
            assert all(item["reason"] != "file_missing" for item in missing)
        finally:
            await session.execute(delete(FileAsset).where(FileAsset.id == asset_id))
            await session.commit()


@pytest.mark.asyncio
async def test_order_linking_round_trip_is_persistent_and_one_to_one() -> None:
    _assert_isolated_test_database()
    suffix = uuid.uuid4().hex[:12]
    admin, password = await _seed_admin(suffix)
    international_model = MODEL_BY_TABLE["international_orders"]
    local_order_id: uuid.UUID | None = None
    international_order_id: uuid.UUID | None = None
    async with SessionFactory() as session:
        local_order = Order(
            order_number=f"QA-LINK-{suffix}",
            user_id=admin.id,
            status="pending",
            total=Decimal("100.00"),
            subtotal=Decimal("100.00"),
            shipping_total=Decimal("0.00"),
            discount_total=Decimal("0.00"),
            currency_code="YER",
            payment_status="pending",
            shipping_address={},
            extra_data={"order_linking_candidate": True},
        )
        international_order = international_model(
            user_id=admin.id,
            status="pending",
            description=f"طلب دولي قابل للربط {suffix}",
            amount=Decimal("100.00"),
            extra_data={"items": [{"product_name": "منتج دولي قابل للربط", "quantity": 1}]},
        )
        session.add_all([local_order, international_order])
        await session.commit()
        local_order_id = local_order.id
        international_order_id = international_order.id

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _login(client, admin.email, password)
            before_link = await client.get("/api/admin-shopping/order-links/international", headers=headers)
            assert before_link.status_code == 200, before_link.text
            before_meta = before_link.json()["meta"]
            assert before_meta["total"] == before_meta["linked"] + before_meta["unlinked"]
            assert any(row["id"] == str(international_order_id) for row in before_link.json()["data"])
            linked = await client.post(
                "/api/admin-shopping/order-links",
                headers=headers,
                json={"localOrderId": str(local_order_id), "internationalOrderId": str(international_order_id)},
            )
            assert linked.status_code == 200, linked.text
            assert linked.json()["data"]["local_order"]["linked_international_order_id"] == str(international_order_id)

            listed = await client.get("/api/admin-shopping/order-links/international", headers=headers)
            assert listed.status_code == 200, listed.text
            linked_intl = next(row for row in listed.json()["data"] if row["id"] == str(international_order_id))
            assert linked_intl["linked_local_order_id"] == str(local_order_id)
            after_meta = listed.json()["meta"]
            assert after_meta["total"] == before_meta["total"]
            assert after_meta["linked"] == before_meta["linked"] + 1
            assert after_meta["unlinked"] == before_meta["unlinked"] - 1

            unlinked = await client.delete(
                f"/api/admin-shopping/order-links/{local_order_id}",
                headers=headers,
            )
            assert unlinked.status_code == 200, unlinked.text

        async with SessionFactory() as session:
            local_after = await session.get(Order, local_order_id)
            international_after = await session.get(international_model, international_order_id)
            assert local_after is not None
            assert international_after is not None
            assert "linked_international_order_id" not in (local_after.extra_data or {})
            assert "linked_local_order_id" not in (international_after.extra_data or {})
    finally:
        async with SessionFactory() as session:
            await session.execute(delete(Order).where(Order.id == local_order_id))
            await session.execute(delete(international_model).where(international_model.id == international_order_id))
            await session.commit()
        await _delete_users([admin.id])


@pytest.mark.asyncio
async def test_backup_queue_commits_request_before_background_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_isolated_test_database()
    suffix = uuid.uuid4().hex[:12]
    admin, password = await _seed_admin(suffix)
    observed = asyncio.Event()
    backup_id: uuid.UUID | None = None

    async def fake_worker(self: BackupCoordinator, queued_id: str, actor_id: str, selected_tables: list[str]) -> None:
        assert actor_id == str(admin.id)
        assert selected_tables == ["orders"]
        async with SessionFactory() as worker_session:
            row = await worker_session.get(MODEL_BY_TABLE["backup_records"], uuid.UUID(queued_id))
            assert row is not None
            assert row.status == "requested"
        observed.set()

    monkeypatch.setattr(BackupCoordinator, "_run_queued_backup", fake_worker)
    coordinator = object.__new__(BackupCoordinator)
    coordinator.settings = get_settings()
    try:
        async with SessionFactory() as session:
            queued = await coordinator.queue_backup(session, actor=admin, selected_tables=["orders"])
            backup_id = uuid.UUID(queued["id"])
            assert queued["status"] == "requested"
            persisted = await session.get(MODEL_BY_TABLE["backup_records"], backup_id)
            assert persisted is not None
            assert persisted.status == "requested"
        await asyncio.wait_for(observed.wait(), timeout=2)
    finally:
        async with SessionFactory() as session:
            if backup_id is not None:
                await session.execute(delete(MODEL_BY_TABLE["backup_records"]).where(MODEL_BY_TABLE["backup_records"].id == backup_id))
            await session.commit()
        await _delete_users([admin.id])


@pytest.mark.asyncio
async def test_blog_create_is_visible_after_a_fresh_admin_read() -> None:
    _assert_isolated_test_database()
    suffix = uuid.uuid4().hex[:12]
    admin, password = await _seed_admin(suffix)
    article_id: uuid.UUID | None = None
    slug = f"qa-blog-{suffix}"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _login(client, admin.email, password)
            created = await client.post(
                "/api/content/blog",
                headers=headers,
                json={
                    "title": f"مقال اختبار {suffix}",
                    "slug": slug,
                    "content": "محتوى مقال الاختبار",
                    "category": "general",
                    "is_published": True,
                },
            )
            assert created.status_code == 201, created.text
            article_id = uuid.UUID(created.json()["data"]["id"])
            assert created.json()["data"]["is_published"] is True

            listed = await client.get("/api/content/blog?admin=true", headers=headers)
            assert listed.status_code == 200, listed.text
            listed_article = next(row for row in listed.json()["data"] if row["id"] == str(article_id))
            assert listed_article["title"] == f"مقال اختبار {suffix}"
            assert listed_article["content"] == "محتوى مقال الاختبار"

            public_article = await client.get(f"/api/content/blog/{slug}")
            assert public_article.status_code == 200, public_article.text
            assert public_article.json()["data"]["id"] == str(article_id)

            updated = await client.patch(
                f"/api/content/blog/{article_id}",
                headers=headers,
                json={"title": f"مقال اختبار معدل {suffix}", "is_published": True},
            )
            assert updated.status_code == 200, updated.text

            fresh_read = await client.get("/api/content/blog?admin=true", headers=headers)
            assert fresh_read.status_code == 200, fresh_read.text
            assert next(row for row in fresh_read.json()["data"] if row["id"] == str(article_id))["title"] == f"مقال اختبار معدل {suffix}"

            deleted = await client.delete(f"/api/content/blog/{article_id}", headers=headers)
            assert deleted.status_code == 200, deleted.text
            after_delete = await client.get("/api/content/blog?admin=true", headers=headers)
            assert all(row["id"] != str(article_id) for row in after_delete.json()["data"])
    finally:
        async with SessionFactory() as session:
            if article_id is not None:
                await session.execute(delete(MODEL_BY_TABLE["blog_articles"]).where(MODEL_BY_TABLE["blog_articles"].id == article_id))
            await session.commit()
        await _delete_users([admin.id])


@pytest.mark.asyncio
async def test_theme_history_and_contact_reply_are_persisted_in_isolated_flow() -> None:
    _assert_isolated_test_database()
    suffix = uuid.uuid4().hex[:12]
    admin, password = await _seed_admin(suffix)
    theme_key = f"qa-four-defects-{suffix}"
    contact_model = MODEL_BY_TABLE["contact_messages"]
    outbox_model = MODEL_BY_TABLE["email_outbox"]
    theme_model = MODEL_BY_TABLE["theme_settings"]
    async with SessionFactory() as session:
        contact = contact_model(
            user_id=admin.id,
            name="عميل اختبار البريد",
            email=f"contact-{suffix}@example.com",
            subject="اختبار الدعم",
            message="رسالة اختبار معزولة",
            status="new",
        )
        session.add(contact)
        await session.commit()
        contact_id = contact.id

    try:
        async with SessionFactory() as session:
            saved = await ThemeAdminService().save(
                session,
                actor=admin,
                roles={"admin"},
                body={"value": {"primary": "43 85% 50%", "qa": suffix}},
                setting_key=theme_key,
                publish=True,
            )
            assert saved["name"] == theme_key
            history = (
                await session.execute(
                    select(theme_model).where(theme_model.name == f"history:{theme_key}:1")
                )
            ).scalar_one()
            assert history.status == "history"
            assert history.extra_data["new_value"]["qa"] == suffix

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _login(client, admin.email, password)
            reply = await client.post(
                f"/api/admin/contact-messages/{contact_id}/reply",
                headers=headers,
                json={"subject": "رد اختبار معزول", "message": "تم تسجيل الرد في قائمة الإرسال."},
            )
            assert reply.status_code == 202, reply.text
            assert reply.json()["queued"] is True
            listed_messages = await client.get("/api/admin/contact-messages", headers=headers)
            assert listed_messages.status_code == 200, listed_messages.text
            replied_message = next(row for row in listed_messages.json()["data"] if row["id"] == str(contact_id))
            assert replied_message["status"] == "replied"
            assert replied_message["is_read"] is True

        async with SessionFactory() as session:
            queued = (
                await session.execute(
                    select(outbox_model).where(
                        outbox_model.email == f"contact-{suffix}@example.com",
                        outbox_model.status == "queued",
                    )
                )
            ).scalar_one()
            assert queued.extra_data["contact_message_id"] == str(contact_id)
    finally:
        async with SessionFactory() as session:
            await session.execute(delete(contact_model).where(contact_model.id == contact_id))
            await session.execute(delete(outbox_model).where(outbox_model.email == f"contact-{suffix}@example.com"))
            await session.execute(delete(theme_model).where(theme_model.name.like(f"%{suffix}%")))
            await session.execute(delete(MODEL_BY_TABLE["audit_logs"]).where(MODEL_BY_TABLE["audit_logs"].user_id == admin.id))
            await session.commit()
        await _delete_users([admin.id])


@pytest.mark.asyncio
async def test_admin_crud_and_payment_lifecycle_in_isolated_flow() -> None:
    _assert_isolated_test_database()
    suffix = uuid.uuid4().hex[:12]
    admin, password = await _seed_admin(suffix)
    color_model = MODEL_BY_TABLE["color_options"]
    local_request_model = MODEL_BY_TABLE["local_shopping_requests"]
    local_request_id: uuid.UUID | None = None
    color_id: uuid.UUID | None = None
    async with SessionFactory() as session:
        local_request = local_request_model(
            user_id=admin.id,
            status="pending",
            description=f"طلب محلي معزول {suffix}",
            amount=0,
            extra_data={"qa": suffix},
        )
        session.add(local_request)
        await session.commit()
        local_request_id = local_request.id

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _login(client, admin.email, password)
            created = await client.post(
                "/api/catalog/admin/options/colors",
                headers=headers,
                json={"name": f"لون معزول {suffix}", "name_en": f"isolated-{suffix}", "hex_code": "#A17712", "sort_order": 999, "is_active": True},
            )
            assert created.status_code == 201, created.text
            color_id = uuid.UUID(created.json()["data"]["id"])
            updated = await client.patch(
                f"/api/catalog/admin/options/colors/{color_id}",
                headers=headers,
                json={"name": f"لون معزول معدل {suffix}", "hex_code": "#A17713"},
            )
            assert updated.status_code == 200, updated.text
            deleted = await client.delete(f"/api/catalog/admin/options/colors/{color_id}", headers=headers)
            assert deleted.status_code == 200, deleted.text

            request_update = await client.patch(
                f"/api/admin-shopping/local-requests/{local_request_id}",
                headers=headers,
                json={"status": "reviewing", "description": f"طلب محلي مراجع {suffix}"},
            )
            assert request_update.status_code == 200, request_update.text

            payment = await client.post(
                f"/api/payments/local/{local_request_id}",
                headers=headers,
                json={"amount": 20, "payment_method": "cash", "currency_code": "YER", "status": "pending", "notes": suffix},
            )
            assert payment.status_code == 201, payment.text
            payment_id = uuid.UUID(payment.json()["data"]["id"])
            payment_update = await client.patch(
                f"/api/payments/local/records/{payment_id}",
                headers=headers,
                json={"status": "confirmed"},
            )
            assert payment_update.status_code == 200, payment_update.text
            payment_delete = await client.delete(f"/api/payments/local/records/{payment_id}", headers=headers)
            assert payment_delete.status_code == 200, payment_delete.text
            listed = await client.get(f"/api/payments/local/{local_request_id}", headers=headers)
            assert listed.status_code == 200
            assert all(row["id"] != str(payment_id) for row in listed.json()["data"])
    finally:
        async with SessionFactory() as session:
            if local_request_id is not None:
                await session.execute(
                    delete(MODEL_BY_TABLE["order_payments"]).where(
                        MODEL_BY_TABLE["order_payments"].extra_data["local_request_id"].astext == str(local_request_id)
                    )
                )
                await session.execute(delete(local_request_model).where(local_request_model.id == local_request_id))
            if color_id is not None:
                await session.execute(delete(color_model).where(color_model.id == color_id))
            await session.commit()
        await _delete_users([admin.id])
