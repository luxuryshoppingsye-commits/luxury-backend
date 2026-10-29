import asyncio
from datetime import date
import importlib.util
from pathlib import Path
import re
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import select, text

from app.config import get_settings
from app.database import engine, SessionFactory
from app.models import MODEL_BY_TABLE
from app.models.domain import DocumentNumberCounter, Order, User
from app.security.passwords import hash_password
from app.services.document_numbers import next_document_number
from app.services.notification_service import NotificationService, NotificationPayload, create_payment_status_notification, _english_notification_content

pytestmark = pytest.mark.asyncio


async def customer():
    assert get_settings().is_test_environment and "127.0.0.1" in get_settings().database_url
    async with SessionFactory() as session:
        user = User(email=f"sara{uuid.uuid4().hex[:8]}@gmail.com", password_hash=hash_password("Admin1234"), is_active=True)
        session.add(user)
        await session.commit()
        return user.id


async def test_concurrent_orders_have_unique_sequential_references():
    owner = await customer()
    async def create():
        async with SessionFactory() as session:
            order = Order(user_id=owner, total=1000, notes="فستان أسود")
            session.add(order)
            await session.commit()
            return order.order_number, order.extra_data["invoice_number"]
    values = await asyncio.gather(*(create() for _ in range(12)))
    for column, prefix in ((0, "ORD"), (1, "INV")):
        numbers = [value[column] for value in values]
        assert len(set(numbers)) == 12
        assert all(re.fullmatch(prefix + r"-\d{6}-\d{4,}", number) for number in numbers)
        sequence = sorted(int(number.rsplit("-", 1)[1]) for number in numbers)
        assert sequence == list(range(sequence[0], sequence[0] + 12))


async def test_counter_resets_by_day_and_prefix_and_exceeds_four_digits():
    async with engine.begin() as connection:
        first, second = date(2098, 1, 1), date(2098, 1, 2)
        counters = DocumentNumberCounter.__table__
        await connection.execute(text("DELETE FROM document_number_counters WHERE number_date IN ('2098-01-01', '2098-01-02')"))
        async def number(prefix, day):
            return await connection.run_sync(lambda sync: next_document_number(sync, counters, prefix, day))
        assert await number("ORD", first) == "ORD-980101-0001"
        assert await number("ORD", first) == "ORD-980101-0002"
        assert await number("INL", first) == "INL-980101-0001"
        assert await number("ORD", second) == "ORD-980102-0001"
        await connection.execute(text("UPDATE document_number_counters SET last_value = 9999 WHERE prefix = 'ORD' AND number_date = '2098-01-01'"))
        assert await number("ORD", first) == "ORD-980101-10000"


@pytest.mark.parametrize("table,prefix", [("orders", "ORD"), ("international_orders", "INL"), ("local_shopping_requests", "LOC")])
async def test_order_numbers_survive_updates_and_notifications_identify_order(table, prefix):
    owner = await customer()
    async with SessionFactory() as session:
        order = MODEL_BY_TABLE[table](user_id=owner, status="pending")
        if table == "orders":
            order.total = 2200
        else:
            order.amount = 2200
        session.add(order)
        await session.flush()
        number = getattr(order, "order_number", None) or order.extra_data["order_number"]
        invoice = order.extra_data["invoice_number"]
        assert number.startswith(prefix + "-") and invoice.startswith("INV-")
        order.status = "confirmed"
        await session.flush()
        assert (getattr(order, "order_number", None) or order.extra_data["order_number"], order.extra_data["invoice_number"]) == (number, invoice)
        service = NotificationService(session)
        status = await service.create_notification(NotificationPayload(
            user_id=owner, title="تحديث حالة الطلب", body="تم تأكيد الطلب", notification_type="order_status",
            entity_type=table, entity_id=str(order.id), payload={"order_status": "confirmed"},
        ))
        payment = await create_payment_status_notification(session, user_id=owner, status="confirmed", international=table == "international_orders",
            entity_type=table, entity_id=str(order.id), action_url="/orders")
        for notification in (status, payment):
            assert number in notification.title and number in notification.body
            assert notification.payload["order_number"] == number
            english = _english_notification_content(notification, {"data": notification.payload})
            assert all(number in value for value in english)
            outbox = (await session.execute(select(MODEL_BY_TABLE["notification_outbox"]).where(
                MODEL_BY_TABLE["notification_outbox"].payload["notification_id"].astext == str(notification.id)
            ))).scalar_one()
            await service._queue_email(outbox, str(notification.id))
            email = (await session.execute(select(MODEL_BY_TABLE["email_outbox"]).where(
                MODEL_BY_TABLE["email_outbox"].extra_data["notification_id"].astext == str(notification.id)
            ))).scalar_one()
            assert number in email.title and number in email.message
        await session.rollback()


async def test_vouchers_share_receipt_and_payment_sequences():
    async with SessionFactory() as session:
        rows = [MODEL_BY_TABLE[table](amount=100, status="pending", **({"type": "payment"} if table == "financial_vouchers" else {}))
                for table in ("order_payments", "international_order_payments", "general_expenses", "financial_vouchers")]
        for row in rows:
            session.add(row)
            await session.flush()
        assert all(row.extra_data["voucher_number"].startswith("RV-") for row in rows[:2])
        assert all(row.extra_data["voucher_number"].startswith("PV-") for row in rows[2:])
        for pair in (rows[:2], rows[2:]):
            assert int(pair[1].extra_data["voucher_number"].rsplit("-", 1)[1]) == int(pair[0].extra_data["voucher_number"].rsplit("-", 1)[1]) + 1
        await session.rollback()


async def test_migration_preserves_old_references_ids_and_financial_values():
    owner = await customer()
    identity = uuid.uuid4()
    migration_path = Path(__file__).parents[2] / "alembic/versions/20261012_01_document_numbers.py"
    spec = importlib.util.spec_from_file_location("document_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(text("""INSERT INTO orders (id, user_id, order_number, total, subtotal, status, payment_status, extra_data)
            VALUES (:id, :owner, 'ORD-20261012-51A1332D', 2200, 2200, 'confirmed', 'partial',
                    '{"paid_amount":"1000.00","remaining_balance":"1200.00"}'::jsonb)"""), {"id": identity, "owner": owner})
        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
        await connection.run_sync(upgrade)
        row = (await connection.execute(text("SELECT id, user_id, order_number, total, status, payment_status, extra_data FROM orders WHERE id=:id"), {"id": identity})).mappings().one()
        assert row["id"] == identity and row["user_id"] == owner and row["total"] == 2200
        assert row["status"] == "confirmed" and row["payment_status"] == "partial"
        assert row["extra_data"]["paid_amount"] == "1000.00" and row["extra_data"]["remaining_balance"] == "1200.00"
        assert row["extra_data"]["previous_order_number"] == "ORD-20261012-51A1332D"
        assert row["extra_data"]["previous_invoice_number"] == "ORD-20261012-51A1332D"
        assert re.fullmatch(r"ORD-\d{6}-\d{4,}", row["order_number"])
        assert re.fullmatch(r"INV-\d{6}-\d{4,}", row["extra_data"]["invoice_number"])
        await transaction.rollback()
