from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.main import app


pytestmark = pytest.mark.asyncio


async def test_malformed_json_returns_client_error_not_500() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/auth/password-reset",
            content='{"broken":',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_json"


async def test_me_password_session_requires_access_token() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/me/password/session", json={})
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication_required"
