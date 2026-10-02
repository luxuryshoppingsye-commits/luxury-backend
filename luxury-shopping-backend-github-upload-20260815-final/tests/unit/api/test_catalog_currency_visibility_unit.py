from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.api.routes.commerce import _catalog_currencies_uncached


@pytest.mark.asyncio
async def test_disabled_currencies_do_not_reappear_as_defaults() -> None:
    active_rows = MagicMock()
    active_rows.scalars.return_value = []
    any_stored_currency = MagicMock()
    any_stored_currency.first.return_value = ("stored-currency",)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[active_rows, any_stored_currency])

    assert await _catalog_currencies_uncached(limit=100, session=session) == {"data": []}
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_fresh_currency_table_keeps_initial_defaults() -> None:
    active_rows = MagicMock()
    active_rows.scalars.return_value = []
    any_stored_currency = MagicMock()
    any_stored_currency.first.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[active_rows, any_stored_currency])

    result = await _catalog_currencies_uncached(limit=100, session=session)

    assert [row["code"] for row in result["data"]] == ["YER", "USD", "SAR"]
