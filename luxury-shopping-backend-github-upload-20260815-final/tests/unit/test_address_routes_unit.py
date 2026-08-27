from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.app.api.routes.auth import _address_boolean, _clear_default_address
from backend.app.models import MODEL_BY_TABLE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
        (None, False),
    ],
)
def test_address_boolean_normalizes_json_and_legacy_values(value, expected):
    assert _address_boolean(value) is expected


@pytest.mark.asyncio
async def test_clear_default_address_flushes_before_new_default_is_written():
    session = AsyncMock()
    model = MODEL_BY_TABLE["customer_addresses"]

    await _clear_default_address(session, model, uuid4())

    session.execute.assert_awaited_once()
    session.flush.assert_awaited_once()
