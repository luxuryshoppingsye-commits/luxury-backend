from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend.app import dependencies
from backend.app.api.routes import commerce, operations
from backend.app.services import staff_permissions


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


@pytest.mark.asyncio
async def test_admin_order_list_requires_orders_view_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid4())
    session = object()
    calls: list[tuple[object, object, set[str], str]] = []

    async def require_permission(received_session, user_id, roles, permission):
        calls.append((received_session, user_id, roles, permission))

    async def visible_orders(*_args, **_kwargs):
        return []

    async def serialize_orders(_session, rows):
        assert rows == []
        return []

    monkeypatch.setattr(commerce, "require_staff_permission", require_permission)
    monkeypatch.setattr(commerce, "_visible_orders", visible_orders)
    monkeypatch.setattr(commerce, "_serialize_orders_with_financials", serialize_orders)

    request = SimpleNamespace(url=SimpleNamespace(path="/api/orders"))
    response = await commerce.orders(
        request=request,
        scope="admin",
        user=user,
        roles={"employee"},
        session=session,
    )

    assert calls == [(session, user.id, {"employee"}, "orders.view")]
    assert response == {"data": []}


@pytest.mark.asyncio
async def test_order_status_update_requires_orders_update_but_pure_courier_keeps_own_path(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid4())
    session = object()
    calls: list[tuple[object, object, set[str], str]] = []

    async def require_permission(received_session, user_id, roles, permission):
        calls.append((received_session, user_id, roles, permission))
        raise HTTPException(status_code=403, detail="staff_permission_denied")

    class Request:
        async def json(self):
            raise RuntimeError("request_body_reached")

    monkeypatch.setattr(commerce, "require_staff_permission", require_permission)

    with pytest.raises(HTTPException) as denied:
        await commerce.change_order_status(uuid4(), Request(), user, {"employee"}, session)
    assert denied.value.status_code == 403
    assert calls == [(session, user.id, {"employee"}, "orders.update")]

    with pytest.raises(RuntimeError, match="request_body_reached"):
        await commerce.change_order_status(uuid4(), Request(), user, {"courier"}, session)
    assert len(calls) == 1

    with pytest.raises(HTTPException) as rollback_denied:
        await commerce.rollback_order_status(uuid4(), user, {"manager"}, session)
    assert rollback_denied.value.status_code == 403
    assert calls[-1] == (session, user.id, {"manager"}, "orders.update")


@pytest.mark.asyncio
async def test_orders_view_alone_cannot_update_order_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def only_view(_session, _user_id, _roles):
        return {"orders.view"}

    monkeypatch.setattr(staff_permissions, "effective_permissions", only_view)
    with pytest.raises(HTTPException) as denied:
        await staff_permissions.require_staff_permission(object(), uuid4(), {"employee"}, "orders.update")
    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "staff_permission_denied", "permission": "orders.update"}


@pytest.mark.asyncio
async def test_order_profile_lookup_uses_orders_view_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    staff = SimpleNamespace(id=uuid4())
    session = object()
    calls: list[tuple[object, object, set[str], str]] = []

    async def require_permission(received_session, user_id, roles, permission):
        calls.append((received_session, user_id, roles, permission))

    class Request:
        async def json(self):
            return {"user_ids": []}

    monkeypatch.setattr(operations, "require_staff_permission", require_permission)

    response = await operations.api_admin_profiles_lookup(
        request=Request(),
        staff=staff,
        roles={"employee"},
        session=session,
    )

    assert calls == [(session, staff.id, {"employee"}, "orders.view")]
    assert response == {"data": []}
