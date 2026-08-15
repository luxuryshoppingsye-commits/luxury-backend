from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend.app import dependencies


@pytest.mark.asyncio
async def test_current_user_requires_authenticated_user() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await dependencies.current_user(None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "authentication_required"


@pytest.mark.asyncio
async def test_current_user_returns_authenticated_user() -> None:
    user = SimpleNamespace(id=uuid4(), is_active=True, deleted_at=None)

    assert await dependencies.current_user(user) is user


@pytest.mark.asyncio
async def test_require_roles_allows_matching_role() -> None:
    user = SimpleNamespace(id=uuid4())
    dependency = dependencies.require_roles("admin", "manager")

    assert await dependency(user=user, roles={"customer", "manager"}) is user


@pytest.mark.asyncio
async def test_require_roles_rejects_missing_role() -> None:
    user = SimpleNamespace(id=uuid4())
    dependency = dependencies.require_roles("admin")

    with pytest.raises(HTTPException) as exc_info:
        await dependency(user=user, roles={"customer"})

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "insufficient_permissions"


@pytest.mark.asyncio
async def test_optional_user_rejects_refresh_token_for_access_protected_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dependencies,
        "decode_token",
        lambda _token: {"sub": str(uuid4()), "type": "refresh"},
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")

    with pytest.raises(HTTPException) as exc_info:
        await dependencies.optional_user(credentials=credentials, session=object())

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_access_token"


@pytest.mark.asyncio
async def test_optional_user_returns_none_without_credentials() -> None:
    assert await dependencies.optional_user(credentials=None, session=object()) is None


@pytest.mark.asyncio
async def test_optional_user_rejects_missing_or_invalid_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")

    for payload in [
        {"type": "access"},
        {"sub": "not-a-uuid", "type": "access"},
    ]:
        monkeypatch.setattr(dependencies, "decode_token", lambda _token, payload=payload: payload)
        with pytest.raises(HTTPException) as exc_info:
            await dependencies.optional_user(credentials=credentials, session=object())
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "invalid_access_token"


@pytest.mark.asyncio
async def test_optional_user_rejects_inactive_loaded_user(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid4()
    monkeypatch.setattr(
        dependencies,
        "decode_token",
        lambda _token: {"sub": str(user_id), "type": "access"},
    )

    class FakeSession:
        async def get(self, _model: object, requested_id: object) -> object:
            assert requested_id == user_id
            return SimpleNamespace(id=requested_id, is_active=False, deleted_at=None)

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")

    with pytest.raises(HTTPException) as exc_info:
        await dependencies.optional_user(credentials=credentials, session=FakeSession())

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "inactive_user"


@pytest.mark.asyncio
async def test_optional_user_returns_active_loaded_user(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid4()
    expected = SimpleNamespace(id=user_id, is_active=True, deleted_at=None)
    monkeypatch.setattr(
        dependencies,
        "decode_token",
        lambda _token: {"sub": str(user_id), "type": "access"},
    )
    async def active_account_security(_session, requested_id, create=False):
        return SimpleNamespace(
            user_id=requested_id,
            account_status="active",
            security_version=0,
        )

    monkeypatch.setattr(dependencies, "account_security_for", active_account_security)

    class FakeSession:
        async def get(self, _model: object, requested_id: object) -> object:
            assert requested_id == user_id
            return expected

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")

    assert await dependencies.optional_user(credentials=credentials, session=FakeSession()) is expected


@pytest.mark.asyncio
async def test_user_roles_reads_roles_for_current_user() -> None:
    user = SimpleNamespace(id=uuid4())

    class Result:
        def scalars(self) -> list[str]:
            return ["customer", "finance", "customer"]

    class FakeSession:
        async def execute(self, statement: object) -> Result:
            self.statement = statement
            return Result()

    session = FakeSession()

    assert await dependencies.user_roles(user=user, session=session) == {"customer", "finance"}
    assert hasattr(session, "statement")


@pytest.mark.asyncio
async def test_user_roles_uses_shared_auth_role_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid4())
    session = object()

    async def fake_roles_for(received_session: object, received_user_id: object) -> list[str]:
        assert received_session is session
        assert received_user_id == user.id
        return ["admin", "manager"]

    monkeypatch.setattr(dependencies, "roles_for", fake_roles_for)

    assert await dependencies.user_roles(user=user, session=session) == {"admin", "manager"}
