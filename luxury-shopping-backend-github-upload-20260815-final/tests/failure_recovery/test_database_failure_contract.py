from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from backend.app.main import sqlalchemy_exception_handler


pytestmark = pytest.mark.asyncio


async def test_database_failure_returns_safe_service_unavailable() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/products",
            "headers": [],
        }
    )

    response = await sqlalchemy_exception_handler(
        request,
        SQLAlchemyError("simulated PostgreSQL outage"),
    )

    assert response.status_code == 503
    assert b"database_unavailable" in response.body
    assert b"simulated PostgreSQL outage" not in response.body
