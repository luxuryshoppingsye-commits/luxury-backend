from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import uuid

import pytest

from backend.app.models.domain import Profile, User
from backend.app.services.report_admin_services import AdminCustomerAccessService


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return (row[0] for row in self._rows)


class _Session:
    def __init__(self, user: User, profile: Profile, address):
        self._user = user
        self._profile = profile
        self._address = address
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _Result([(self._user, self._profile)])
        if len(self.statements) == 2:
            return _Result([(self._address,)])
        return _Result(
            [
                (
                    self._user.id,
                    2,
                    Decimal("1250.50"),
                    datetime(2026, 9, 14, 8, 30, tzinfo=timezone.utc),
                )
            ]
        )


@pytest.mark.asyncio
async def test_customer_list_exposes_order_stats_from_orders_table() -> None:
    user_id = uuid.uuid4()
    created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    user = User(
        id=user_id,
        email="customer@example.com",
        password_hash="test",
        is_active=True,
        created_at=created_at,
    )
    profile = Profile(id=user_id, user_id=user_id, full_name="Customer")
    address = SimpleNamespace(
        user_id=user_id,
        city="صنعاء",
        governorate="أمانة العاصمة",
    )
    session = _Session(user, profile, address)

    rows = await AdminCustomerAccessService.list_customers(
        session,
        roles={"finance"},
        full=False,
    )

    assert len(session.statements) == 3
    assert "orders" in str(session.statements[2]).lower()
    assert rows[0]["city"] == "صنعاء"
    assert rows[0]["governorate"] == "أمانة العاصمة"
    assert rows[0]["order_count"] == 2
    assert rows[0]["total_orders"] == 2
    assert rows[0]["total_spent"] == "1250.50"
    assert rows[0]["last_order_date"] == "2026-09-14T08:30:00+00:00"
