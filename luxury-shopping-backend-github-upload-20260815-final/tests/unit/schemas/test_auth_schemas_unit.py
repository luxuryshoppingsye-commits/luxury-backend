from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
)


def test_login_request_accepts_existing_short_legacy_passwords() -> None:
    payload = LoginRequest.model_validate(
        {"email": "customer@luxury-unit.com", "password": "x"}
    )

    assert payload.email == "customer@luxury-unit.com"
    assert payload.password == "x"


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": "ValidPass123"},
        {"email": "customer@luxury-unit.com", "password": ""},
    ],
)
def test_login_request_rejects_invalid_payloads(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        LoginRequest.model_validate(payload)


def test_register_request_accepts_aliases_and_normalizes_phone() -> None:
    payload = RegisterRequest.model_validate(
        {
            "email": "new.customer@luxury-unit.com",
            "password": "ValidPass123",
            "fullName": "CODEX Unit Customer",
            "phone": "+967 777-123456",
            "city": "Sanaa",
        }
    )

    assert payload.full_name == "CODEX Unit Customer"
    assert payload.phone == "+967777123456"
    assert payload.city == "Sanaa"


@pytest.mark.parametrize(
    "phone",
    ["abc", "1234567", "+9677771234567899", "++967777123456"],
)
def test_register_request_rejects_invalid_phone_values(phone: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(
            {
                "email": "new.customer@luxury-unit.com",
                "password": "ValidPass123",
                "fullName": "CODEX Unit Customer",
                "phone": phone,
            }
        )


def test_register_request_allows_blank_optional_phone_as_none() -> None:
    payload = RegisterRequest.model_validate(
        {
            "email": "new.customer@luxury-unit.com",
            "password": "ValidPass123",
            "fullName": "CODEX Unit Customer",
            "phone": " ",
        }
    )

    assert payload.phone is None


def test_refresh_request_requires_a_real_refresh_token() -> None:
    assert RefreshRequest.model_validate({"refreshToken": "x" * 20}).refresh_token == "x" * 20
    with pytest.raises(ValidationError):
        RefreshRequest.model_validate({"refreshToken": "too-short"})


def test_password_change_request_uses_api_aliases() -> None:
    payload = PasswordChangeRequest.model_validate(
        {"currentPassword": "OldPass123", "newPassword": "NewPass123"}
    )

    assert payload.current_password == "OldPass123"
    assert payload.new_password == "NewPass123"


def test_password_reset_request_uses_redirect_alias_and_forbids_extra_fields() -> None:
    payload = PasswordResetRequest.model_validate(
        {
            "email": "reset@example.com",
            "redirectTo": "http://127.0.0.1:5190/reset-password",
            "clientType": "web",
        }
    )

    assert str(payload.email) == "reset@example.com"
    assert payload.redirect_to == "http://127.0.0.1:5190/reset-password"
    assert payload.client_type == "web"

    with pytest.raises(ValidationError):
        PasswordResetRequest.model_validate(
            {"email": "reset@example.com", "redirectTo": "http://127.0.0.1:5190/reset-password", "role": "admin"}
        )


def test_password_reset_confirm_requires_long_token_and_new_password_alias() -> None:
    payload = PasswordResetConfirm.model_validate(
        {"token": "t" * 32, "newPassword": "NewPass123"}
    )

    assert payload.token == "t" * 32
    with pytest.raises(ValidationError):
        PasswordResetConfirm.model_validate({"token": "short", "newPassword": "NewPass123"})
