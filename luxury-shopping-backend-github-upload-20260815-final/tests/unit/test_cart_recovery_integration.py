import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.app.models import MODEL_BY_TABLE
from backend.app.models.base import Base
from backend.app.models.domain import User, Product, UserCart, Order
from backend.app.services.cart_recovery_service import process_cart_recovery, recovery_stage, cart_recovery_is_current
from backend.app.services.financial_calculator import _coupon_discount, calculate_checkout_financials
from backend.app.services import financial_calculator
from backend.app.services.function_service import _coupon_payload
from backend.app.services.notification_service import NotificationService
from backend.app.services.realtime import RealtimeEventService, realtime_hub


@pytest.mark.parametrize("minutes,expected", [(59, None), (60, "cart_reminder"), (479, "cart_reminder"), (480, "cart_discount")])
def test_recovery_timing(minutes, expected):
    now = datetime.now(timezone.utc)
    assert recovery_stage(now - timedelta(minutes=minutes), now) == expected


@pytest.mark.asyncio
async def test_cart_recovery_postgres_contract(monkeypatch):
    url = os.getenv("CART_RECOVERY_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("Requires an isolated cart-recovery test database")
    assert "127.0.0.1:55439/luxury_cart_test" in url
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(RealtimeEventService, "record_event", AsyncMock(return_value={}))
    monkeypatch.setattr(realtime_hub, "publish_recorded_event", AsyncMock())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            now = datetime.now(timezone.utc)
            user = User(email=f"{uuid.uuid4()}@example.test", password_hash="test", is_active=True)
            other = User(email=f"{uuid.uuid4()}@example.test", password_hash="test", is_active=True)
            product = Product(name="Test product", price=100, stock_quantity=10)
            session.add_all([user, other, product])
            await session.flush()
            activity = now - timedelta(minutes=59)
            cart = UserCart(user_id=user.id, product_id=product.id, quantity=1, created_at=activity, updated_at=activity)
            session.add(cart)
            await session.flush()
            assert (await process_cart_recovery(session, now=now))["total"] == 0
            assert (await process_cart_recovery(session, now=now + timedelta(minutes=1)))["total"] == 1
            assert (await process_cart_recovery(session, now=now + timedelta(minutes=2)))["total"] == 0
            assert (await process_cart_recovery(session, now=now + timedelta(hours=7, minutes=1)))["total"] == 1
            assert (await process_cart_recovery(session, now=now + timedelta(hours=9)))["total"] == 0
            notices = (await session.execute(select(MODEL_BY_TABLE["notifications"]).where(MODEL_BY_TABLE["notifications"].user_id == user.id))).scalars().all()
            assert sorted(n.type for n in notices) == ["cart_discount", "cart_reminder"]
            coupon = (await session.execute(select(MODEL_BY_TABLE["coupons"]).where(MODEL_BY_TABLE["coupons"].extra_data["exclusive_user_id"].astext == str(user.id)))).scalar_one()
            assert coupon.expires_at == now + timedelta(hours=31, minutes=1)
            discount, _, metadata = await _coupon_discount(session, code=coupon.code, subtotal=Decimal(100), user_id=user.id)
            assert discount == 0 and metadata["free_shipping"] is True
            monkeypatch.setattr(financial_calculator, "_shipping_total", AsyncMock(return_value=(Decimal("25"), "test", {})))
            totals = await calculate_checkout_financials(session, user_id=user.id, subtotal=Decimal(100), body={"couponCode": coupon.code})
            assert totals.shipping_total == 0 and totals.total == Decimal(100)
            async with factory() as concurrent:
                assert (await process_cart_recovery(concurrent, now=now + timedelta(hours=9)))["total"] == 0
            with pytest.raises(HTTPException) as error:
                await _coupon_discount(session, code=coupon.code, subtotal=Decimal(100), user_id=other.id)
            assert error.value.detail == "coupon_audience_not_eligible"
            assert (await _coupon_payload(session, {"code": coupon.code, "subtotal": 100}, other))["valid"] is False
            queued = (await session.execute(select(MODEL_BY_TABLE["notification_outbox"]).where(MODEL_BY_TABLE["notification_outbox"].user_id == user.id, MODEL_BY_TABLE["notification_outbox"].type == "cart_discount"))).scalar_one()
            assert await cart_recovery_is_current(session, user.id, queued.payload)
            stale = {**queued.payload, "cart_activity": (activity - timedelta(seconds=1)).isoformat()}
            assert not await cart_recovery_is_current(session, user.id, stale)
            expired = {**queued.payload, "recovery_expires_at": (now - timedelta(seconds=1)).isoformat()}
            assert not await cart_recovery_is_current(session, user.id, expired)
            order = Order(user_id=user.id, order_number=f"QA-{uuid.uuid4()}", status="pending", total=100, created_at=now)
            session.add(order)
            await session.flush()
            assert (await process_cart_recovery(session, now=now + timedelta(hours=10)))["total"] == 0
            result = await NotificationService(session)._deliver_outbox(queued)
            assert result["suppressed"] is True
            session.add(MODEL_BY_TABLE["coupon_usage"](user_id=user.id, order_id=order.id, amount=0, extra_data={"coupon_id": str(coupon.id)}))
            await session.flush()
            with pytest.raises(HTTPException) as error:
                await _coupon_discount(session, code=coupon.code, subtotal=Decimal(100), user_id=user.id)
            assert error.value.detail == "coupon_usage_limit"
            coupon.expires_at = now - timedelta(seconds=1)
            with pytest.raises(HTTPException) as error:
                await _coupon_discount(session, code=coupon.code, subtotal=Decimal(100), user_id=user.id)
            assert error.value.detail == "coupon_expired"
            from backend.app.api.routes.auth import _welcome_customer
            await _welcome_customer(session, user.id)
            await _welcome_customer(session, user.id)
            welcome_rows = (await session.execute(select(MODEL_BY_TABLE["notifications"]).where(MODEL_BY_TABLE["notifications"].user_id == user.id, MODEL_BY_TABLE["notifications"].type == "customer_welcome"))).scalars().all()
            assert len(welcome_rows) == 1
            await session.rollback()
    finally:
        await engine.dispose()
