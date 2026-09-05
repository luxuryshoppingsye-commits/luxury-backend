from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.models.domain import AccountSecurity, LoginAttempt, Profile, RefreshToken, RefreshTokenSecurity, User, UserRole
from backend.app.services import auth_service


def _user(
    *,
    user_id: uuid.UUID | None = None,
    email: str = "user@example.test",
    active: bool = True,
    deleted: bool = False,
) -> User:
    user = User(
        id=user_id or uuid.uuid4(),
        email=email,
        password_hash="stored-hash",
        password_salt=None,
        is_active=active,
    )
    if deleted:
        user.deleted_at = datetime.now(timezone.utc)
    return user


def _profile(user: User) -> Profile:
    return Profile(
        id=user.id,
        user_id=user.id,
        email=user.email,
        full_name="Unit User",
        phone="777123456",
        city="Sanaa",
    )


def _account_security(user: User) -> AccountSecurity:
    return AccountSecurity(user_id=user.id, account_status="active", security_version=0)


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        headers={"user-agent": "unit-agent"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


@pytest.mark.asyncio
async def test_roles_for_returns_roles_from_session() -> None:
    session = _FakeSession([_Result(scalars=["admin", "finance"])])

    assert await auth_service.roles_for(session, uuid.uuid4()) == ["admin", "finance"]


@pytest.mark.asyncio
async def test_roles_for_falls_back_to_profile_role_metadata() -> None:
    user_id = uuid.uuid4()
    profile = Profile(
        id=user_id,
        user_id=user_id,
        email="admin@example.test",
        full_name="Admin User",
        classification="administrator",
        extra_data={"roles": ["finance", "merchant"]},
    )
    session = _FakeSession([
        _Result(scalars=[]),
        _Result(scalar=profile),
    ])

    assert await auth_service.roles_for(session, user_id) == ["admin", "finance", "partner"]


@pytest.mark.asyncio
async def test_roles_for_falls_back_to_bootstrap_admin_email(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(bootstrap_admin_emails="admin@example.test, owner@example.test"),
    )
    session = _FakeSession([
        _Result(scalars=[]),
        _Result(scalar=None),
        _Result(scalar="Admin@Example.Test"),
    ])

    assert await auth_service.roles_for(session, user_id) == ["admin"]


@pytest.mark.asyncio
async def test_roles_for_does_not_bootstrap_unlisted_email(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(bootstrap_admin_emails="admin@example.test"),
    )
    session = _FakeSession([
        _Result(scalars=[]),
        _Result(scalar=None),
        _Result(scalar="customer@example.test"),
    ])

    assert await auth_service.roles_for(session, user_id) == []


@pytest.mark.asyncio
async def test_auth_payload_issues_tokens_and_persists_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(jwt_access_token_minutes=15))
    monkeypatch.setattr(
        auth_service,
        "create_refresh_token",
        lambda: ("raw-refresh", "refresh-digest", datetime.now(timezone.utc) + timedelta(days=7)),
    )
    monkeypatch.setattr(
        auth_service,
        "create_access_token",
        lambda subject, roles, security_version=0: f"access:{subject}:{','.join(roles)}:{security_version}",
    )
    session = _FakeSession([
        _Result(scalars=["admin"]),
        _Result(scalar=_account_security(user)),
        _Result(scalar=_profile(user)),
    ])

    payload = await auth_service.auth_payload(session, user, request=_request())

    assert payload["user"] == {
        "id": str(user.id),
        "email": user.email,
        "is_active": True,
        "role": "admin",
        "roles": ["admin"],
        "account_status": "active",
        "email_verified_at": None,
        "phone_verified_at": None,
        "security_version": 0,
    }
    assert payload["roles"] == ["admin"]
    assert payload["access_token"] == f"access:{user.id}:admin:0"
    assert payload["refresh_token"] == "raw-refresh"
    assert payload["expires_in"] == 900
    refresh = _only_added(session, RefreshToken)
    assert refresh.user_id == user.id
    assert refresh.token_hash == "refresh-digest"
    assert refresh.user_agent == "unit-agent"
    assert refresh.ip_address == "127.0.0.1"
    _only_added(session, RefreshTokenSecurity)
    assert session.flushed == 2


@pytest.mark.asyncio
async def test_auth_payload_can_return_read_only_identity_without_tokens() -> None:
    user = _user()
    session = _FakeSession([
        _Result(scalars=["customer"]),
        _Result(scalar=_account_security(user)),
        _Result(scalar=None),
    ])

    payload = await auth_service.auth_payload(session, user, issue_tokens=False)

    assert payload == {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "is_active": True,
            "role": "customer",
            "roles": ["customer"],
            "account_status": "active",
            "email_verified_at": None,
            "phone_verified_at": None,
            "security_version": 0,
        },
        "profile": None,
        "roles": ["customer"],
    }
    assert session.added == []
    assert session.flushed == 0


@pytest.mark.asyncio
async def test_auth_payload_keeps_access_login_when_optional_session_tables_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()

    async def optional_table_ready(_session, table_name: str) -> bool:
        return table_name not in {
            AccountSecurity.__tablename__,
            RefreshToken.__tablename__,
            RefreshTokenSecurity.__tablename__,
        }

    monkeypatch.setattr(auth_service, "_optional_table_ready", optional_table_ready)
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(jwt_access_token_minutes=15))
    monkeypatch.setattr(
        auth_service,
        "create_access_token",
        lambda subject, roles, security_version=0: f"access:{subject}:{','.join(roles)}:{security_version}",
    )
    session = _FakeSession([
        _Result(scalars=["admin"]),
        _Result(scalar=_profile(user)),
    ])

    payload = await auth_service.auth_payload(session, user, request=_request())

    assert payload["access_token"] == f"access:{user.id}:admin:0"
    assert payload["user"]["account_status"] == "active"
    assert payload["user"]["security_version"] == 0
    assert "refresh_token" not in payload
    assert session.added == []
    assert session.flushed == 0


@pytest.mark.asyncio
async def test_check_login_rate_limit_rejects_too_many_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(login_rate_limit=3))
    session = _FakeSession([_Result(scalar=3)])

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.check_login_rate_limit(session, "user@example.test", "127.0.0.1")

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "too_many_login_attempts"


@pytest.mark.asyncio
async def test_check_action_rate_limit_rejects_email_or_ip_bursts() -> None:
    session = _FakeSession([_Result(scalar=10)])

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.check_action_rate_limit(
            session,
            email="burst@example.test",
            ip="203.0.113.9",
            detail="registration",
            maximum=10,
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "too_many_registration_attempts"


def test_extract_client_ip_trusts_forwarded_for_only_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(trusted_proxy_set={"10.0.0.1"}))
    trusted = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.12, 198.51.100.8"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    spoofed = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.12"},
        client=SimpleNamespace(host="198.51.100.44"),
    )

    assert auth_service.extract_client_ip(trusted) == "203.0.113.12"
    assert auth_service.extract_client_ip(spoofed) == "198.51.100.44"


@pytest.mark.asyncio
async def test_authenticate_records_bad_password_and_reports_password_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user(email="user@example.test")
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(login_rate_limit=5))
    monkeypatch.setattr(auth_service, "verify_password", lambda *_args: (False, False))
    session = _FakeSession([
        _Result(scalar=0),
        _Result(scalar=user),
    ])

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.authenticate(session, " USER@example.test ", "bad", "127.0.0.1")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_password"
    attempt = _only_added(session, LoginAttempt)
    assert attempt.email == "user@example.test"
    assert attempt.succeeded is False
    assert attempt.detail == "bad_password"


@pytest.mark.asyncio
async def test_authenticate_success_rehashes_legacy_password_and_records_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user(email="user@example.test")
    user.password_salt = "legacy-salt"
    user.password_must_reset = True
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(login_rate_limit=5))
    monkeypatch.setattr(auth_service, "verify_password", lambda *_args: (True, True))
    monkeypatch.setattr(auth_service, "hash_password", lambda _password: "new-secure-hash")
    session = _FakeSession([
        _Result(scalar=0),
        _Result(scalar=user),
    ])

    result = await auth_service.authenticate(session, "user@example.test", "Valid123", "127.0.0.1")

    assert result is user
    assert user.password_hash == "new-secure-hash"
    assert user.password_salt is None
    assert user.password_must_reset is False
    assert user.last_login_at is not None
    attempt = _only_added(session, LoginAttempt)
    assert attempt.succeeded is True


@pytest.mark.asyncio
async def test_authenticate_success_without_rehash_keeps_existing_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user(email="user@example.test")
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(login_rate_limit=5))
    monkeypatch.setattr(auth_service, "verify_password", lambda *_args: (True, False))
    session = _FakeSession([
        _Result(scalar=0),
        _Result(scalar=user),
    ])

    result = await auth_service.authenticate(session, "user@example.test", "Valid123", "127.0.0.1")

    assert result is user
    assert user.password_hash == "stored-hash"
    assert user.last_login_at is not None
    assert _only_added(session, LoginAttempt).succeeded is True


@pytest.mark.asyncio
async def test_authenticate_succeeds_when_optional_security_tables_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user(email="user@example.test")

    async def optional_table_ready(_session, table_name: str) -> bool:
        return table_name not in {
            AccountSecurity.__tablename__,
            LoginAttempt.__tablename__,
        }

    monkeypatch.setattr(auth_service, "_optional_table_ready", optional_table_ready)
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(login_rate_limit=5))
    monkeypatch.setattr(auth_service, "verify_password", lambda *_args: (True, False))
    session = _FakeSession([_Result(scalar=user)])

    result = await auth_service.authenticate(session, " USER@example.test ", "Valid123", "127.0.0.1")

    assert result is user
    assert user.last_login_at is not None
    assert session.added == []
    assert session.flushed == 0


@pytest.mark.asyncio
async def test_authenticate_reports_unknown_or_unavailable_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(login_rate_limit=5))
    for candidate, expected_detail in [
        (None, "email_not_registered"),
        (_user(active=False), "account_unavailable"),
        (_user(deleted=True), "account_unavailable"),
    ]:
        session = _FakeSession([
            _Result(scalar=0),
            _Result(scalar=candidate),
        ])
        with pytest.raises(HTTPException) as exc_info:
            await auth_service.authenticate(session, "ghost@example.test", "Valid123", "10.0.0.1")
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == expected_detail
        assert _only_added(session, LoginAttempt).detail == "unknown_or_inactive_user"


@pytest.mark.asyncio
async def test_create_user_normalizes_email_and_creates_profile_and_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_service, "validate_password", lambda password, **_kwargs: None)
    monkeypatch.setattr(auth_service, "hash_password", lambda password: "hashed-password")
    session = _FakeSession([_Result(scalar=None)])

    user = await auth_service.create_user(
        session,
        email=" New@Example.Test ",
        password="Valid123",
        full_name=" New User ",
        phone="777123456",
        city="Sanaa",
        role="partner",
    )

    assert user.email == "new@example.test"
    assert user.password_hash == "hashed-password"
    assert user.is_active is True
    profile = _only_added(session, Profile)
    assert profile.user_id == user.id
    assert profile.full_name == "New User"
    assert profile.email == "new@example.test"
    role = _only_added(session, UserRole)
    assert role.user_id == user.id
    assert role.role == "partner"
    assert session.flushed == 2


@pytest.mark.asyncio
async def test_create_user_rejects_duplicate_email() -> None:
    session = _FakeSession([_Result(scalar=uuid.uuid4())])

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.create_user(
            session,
            email="duplicate@example.test",
            password="Valid123",
            full_name="Duplicate",
            phone=None,
            city=None,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "email_exists"
    assert session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account_status", ["active", "pending_merchant_review"])
async def test_rotate_refresh_token_revokes_old_token_and_links_replacement(
    monkeypatch: pytest.MonkeyPatch, account_status: str,
) -> None:
    user = _user(email="refresh@example.test")
    stored = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash="hash:old",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        revoked_at=None,
    )
    replacement_id = uuid.uuid4()
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(jwt_access_token_minutes=10))
    monkeypatch.setattr(auth_service, "token_hash", lambda raw: f"hash:{raw}")
    monkeypatch.setattr(
        auth_service,
        "create_refresh_token",
        lambda: ("new", "hash:new", datetime.now(timezone.utc) + timedelta(days=7)),
    )
    monkeypatch.setattr(auth_service, "create_access_token", lambda subject, roles, security_version=0: "new-access")
    session = _FakeSession([
        _Result(scalar=stored),
        _Result(scalar=AccountSecurity(user_id=user.id, account_status=account_status, security_version=0)),
        _Result(scalars=["customer"]),
        _Result(scalar=AccountSecurity(user_id=user.id, account_status=account_status, security_version=0)),
        _Result(scalar=_profile(user)),
        _Result(scalar=replacement_id),
    ])
    session.get_result = user

    payload = await auth_service.rotate_refresh_token(session, "old", _request())

    assert payload["access_token"] == "new-access"
    assert payload["refresh_token"] == "new"
    assert stored.revoked_at is not None
    assert stored.replaced_by_id == replacement_id


@pytest.mark.asyncio
async def test_rotate_refresh_token_rejects_missing_revoked_expired_or_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_service, "token_hash", lambda raw: f"hash:{raw}")
    user = _user(active=False)
    expired = RefreshToken(
        user_id=user.id,
        token_hash="hash:old",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    revoked = RefreshToken(
        user_id=user.id,
        token_hash="hash:old",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        revoked_at=datetime.now(timezone.utc),
    )

    for stored, get_result, detail in [
        (None, None, "invalid_refresh_token"),
        (revoked, user, "refresh_token_reuse_detected"),
        (expired, user, "invalid_refresh_token"),
        (
            RefreshToken(
                user_id=user.id,
                token_hash="hash:old",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            ),
            user,
            "inactive_user",
        ),
    ]:
        session = _FakeSession([_Result(scalar=stored)])
        session.get_result = get_result
        with pytest.raises(HTTPException) as exc_info:
            await auth_service.rotate_refresh_token(session, "old", _request())
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == detail


@pytest.mark.asyncio
async def test_revoke_refresh_token_marks_matching_active_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_service, "token_hash", lambda raw: f"hash:{raw}")
    session = _FakeSession([])

    await auth_service.revoke_refresh_token(session, "raw-token")

    assert len(session.executed) == 1


class _Result:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = list(scalars or [])

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return list(self._scalars)


class _FakeSession:
    def __init__(self, execute_results):
        self.execute_results = list(execute_results)
        self.executed = []
        self.added = []
        self.flushed = 0
        self.get_result = None

    async def execute(self, statement):
        self.executed.append(statement)
        if not self.execute_results:
            return _Result(scalar=None)
        return self.execute_results.pop(0)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flushed += 1

    async def get(self, model, identity):
        return self.get_result


def _only_added(session: _FakeSession, model):
    matches = [item for item in session.added if isinstance(item, model)]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize("status,allowed", [
    ("active", True), ("pending_merchant_review", True),
    ("pending_email_verification", False), ("disabled", False),
    ("deleted", False), ("deletion_pending", False), ("merchant_rejected", False),
])
def test_merchant_review_does_not_block_customer_account(status, allowed):
    user = _user()
    state = AccountSecurity(user_id=user.id, account_status=status)
    assert auth_service.account_can_login(user, state) is allowed
    user.is_active = False
    assert auth_service.account_can_login(user, state) is False


@pytest.mark.asyncio
async def test_pending_merchant_authenticates_as_existing_customer(monkeypatch):
    user = _user()
    state = AccountSecurity(user_id=user.id, account_status="pending_merchant_review", security_version=8)
    async def security(*args, **kwargs):
        return state
    monkeypatch.setattr(auth_service, "account_security_for", security)
    monkeypatch.setattr(auth_service, "get_settings", lambda: SimpleNamespace(login_rate_limit=5))
    monkeypatch.setattr(auth_service, "verify_password", lambda *_: (True, False))
    session = _FakeSession([_Result(scalar=0), _Result(scalar=user)])
    assert await auth_service.authenticate(session, user.email, "Valid123", "127.0.0.1") is user
    assert state.security_version == 8
    assert state.account_status == "pending_merchant_review"


@pytest.mark.asyncio
async def test_pending_merchant_access_token_keeps_customer_access(monkeypatch):
    from backend.app import dependencies
    from fastapi.security import HTTPAuthorizationCredentials
    user = _user()
    state = AccountSecurity(user_id=user.id, account_status="pending_merchant_review", security_version=8)
    async def security(*args, **kwargs):
        return state
    monkeypatch.setattr(dependencies, "account_security_for", security)
    monkeypatch.setattr(dependencies, "decode_token", lambda _: {"sub": str(user.id), "type": "access", "sv": 8})
    session = _FakeSession([])
    session.get_result = user
    assert await dependencies.optional_user(credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="test"), session=session) is user
    with pytest.raises(HTTPException) as error:
        await dependencies.require_partner(user=user, roles={"customer"})
    assert error.value.status_code == 403
    state.security_version = 9
    with pytest.raises(HTTPException) as error:
        await dependencies.optional_user(credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="test"), session=session)
    assert error.value.detail == "stale_access_token"


@pytest.mark.asyncio
async def test_register_merchant_preserves_current_customer_session(monkeypatch):
    from backend.app.api.routes import auth
    user = _user()
    state = AccountSecurity(user_id=user.id, account_status="active", security_version=8)
    class Session(_FakeSession):
        async def commit(self):
            pass
    class Request:
        async def json(self):
            return {"storeName": "Test Store"}
    session = Session([_Result(scalar=_profile(user)), _Result(), _Result()])
    async def security(*args, **kwargs):
        return state
    async def event(*args, **kwargs):
        pass
    async def payload(*args, **kwargs):
        return {"user": {"id": str(user.id)}, "roles": ["customer"]}
    async def forbidden_bump(*args, **kwargs):
        pytest.fail("Submitting a merchant application must not revoke the customer session")
    monkeypatch.setattr(auth, "account_security_for", security)
    monkeypatch.setattr(auth, "record_security_event", event)
    monkeypatch.setattr(auth, "auth_payload", payload)
    monkeypatch.setattr(auth, "bump_security_version", forbidden_bump)
    result = await auth.register_merchant(Request(), user=user, session=session)
    assert state.security_version == 8
    assert auth_service.account_can_login(user, state)
    assert result["roles"] == ["customer"]
    assert result["merchant_portal_enabled"] is False
    assert result["application_status"] == "pending"
