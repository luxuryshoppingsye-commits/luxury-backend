from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from backend.app.api.routes import auth


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google.com", "apple.com"])
@pytest.mark.parametrize("existing", [False, True])
async def test_only_new_social_accounts_require_completion(monkeypatch, provider, existing):
    user = SimpleNamespace(id=uuid.uuid4(), deleted_at=None, is_active=True)
    profile = SimpleNamespace(full_name="Customer", phone=None, city=None, extra_data={})
    result = lambda value: SimpleNamespace(scalar_one_or_none=lambda: value)
    session = SimpleNamespace(execute=AsyncMock(side_effect=[result(user if existing else None), result(profile)]), commit=AsyncMock())
    monkeypatch.setattr(auth, "verify_firebase_id_token", AsyncMock(return_value={
        "email": "customer@example.test", "email_verified": True, "uid": "firebase-test", "firebase": {"sign_in_provider": provider}}))
    async def create(*args, **kwargs):
        profile.extra_data = kwargs["extra_data"]
        return user
    creator = AsyncMock(side_effect=create)
    monkeypatch.setattr(auth, "create_user", creator)
    monkeypatch.setattr(auth, "account_security_for", AsyncMock(return_value=SimpleNamespace(account_status="active", email_verified_at=None)))
    monkeypatch.setattr(auth, "record_login_attempt", AsyncMock())
    monkeypatch.setattr(auth, "record_security_event", AsyncMock())
    monkeypatch.setattr(auth, "auth_payload", AsyncMock(return_value={"user": {"id": str(user.id)}}))
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    body = auth.FirebaseAuthRequest(idToken="synthetic-test-token-long-enough", provider=provider)
    await auth._firebase_auth_payload(body, request, session)
    assert bool(profile.extra_data.get("social_profile_completion_required")) is not existing
    assert creator.await_count == (0 if existing else 1)
    assert profile.extra_data["auth_provider"] == provider


@pytest.mark.asyncio
@pytest.mark.parametrize("phone,city,complete", [("777123456", "Sanaa Street", True), (None, "Sanaa", False), ("bad", "Sanaa", False), ("777123456", "", False)])
async def test_completion_marker_clears_only_after_contact_details_save(monkeypatch, phone, city, complete):
    profile = SimpleNamespace(full_name="Customer", phone=None, city=None,
        extra_data={"social_profile_completion_required": True, "auth_provider": "google.com"})
    result = SimpleNamespace(scalar_one=lambda: profile)
    session = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    request = SimpleNamespace(json=AsyncMock(return_value={"full_name": "Customer", "phone": phone, "city": city}))
    monkeypatch.setattr(auth, "auth_payload", AsyncMock(return_value={}))
    if phone == "bad":
        with pytest.raises(auth.HTTPException) as error:
            await auth.update_me(request, SimpleNamespace(id=uuid.uuid4()), session)
        assert error.value.status_code == 422
        session.commit.assert_not_awaited()
    else:
        await auth.update_me(request, SimpleNamespace(id=uuid.uuid4()), session)
    assert profile.extra_data["social_profile_completion_required"] is not complete
    assert profile.extra_data["auth_provider"] == "google.com"
