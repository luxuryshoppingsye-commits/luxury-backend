from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


def normalize_phone_number(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    compact = value.replace(" ", "").replace("-", "")
    if compact.startswith("+"):
        digits = compact[1:]
    else:
        digits = compact
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise ValueError("invalid phone number")
    return compact


class LoginRequest(BaseModel):
    email: EmailStr
    # Existing accounts may have legacy passwords shorter than the new policy.
    # Strength is enforced for registration and password changes, not login.
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(min_length=2, max_length=240)
    phone: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=160)
    gender: str | None = Field(default=None, max_length=32)
    street: str | None = Field(default=None, max_length=160)
    address_details: str | None = Field(default=None, max_length=500)
    captcha_token: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "fullName" in data and "full_name" not in data:
                data["full_name"] = data.pop("fullName")
            if "addressDetails" in data and "address_details" not in data:
                data["address_details"] = data.pop("addressDetails")
            if "captchaToken" in data and "captcha_token" not in data:
                data["captcha_token"] = data.pop("captchaToken")
        return data

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return normalize_phone_number(value)


class FirebaseAuthRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id_token: str = Field(min_length=20, max_length=8192)
    provider: str | None = Field(default=None, max_length=80)
    full_name: str | None = Field(default=None, max_length=240)
    phone: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=160)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "idToken" in data and "id_token" not in data:
                data["id_token"] = data.pop("idToken")
            if "fullName" in data and "full_name" not in data:
                data["full_name"] = data.pop("fullName")
        return data

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return normalize_phone_number(value)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    refresh_token: str = Field(min_length=20)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case(cls, data: Any) -> Any:
        if isinstance(data, dict) and "refreshToken" in data and "refresh_token" not in data:
            data = dict(data)
            data["refresh_token"] = data.pop("refreshToken")
        return data


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "currentPassword" in data and "current_password" not in data:
                data["current_password"] = data.pop("currentPassword")
            if "newPassword" in data and "new_password" not in data:
                data["new_password"] = data.pop("newPassword")
        return data


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    email: EmailStr
    redirect_to: str | None = Field(default=None, max_length=2000)
    client_type: str | None = Field(default=None, max_length=64)
    captcha_token: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "redirectTo" in data and "redirect_to" not in data:
                data["redirect_to"] = data.pop("redirectTo")
            if "clientType" in data and "client_type" not in data:
                data["client_type"] = data.pop("clientType")
            if "captchaToken" in data and "captcha_token" not in data:
                data["captcha_token"] = data.pop("captchaToken")
        return data


class PasswordResetConfirm(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    token: str = Field(min_length=32, max_length=512)
    new_password: str = Field(min_length=8, max_length=256)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_password_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "newPassword" not in data and "new_password" not in data and "password" in data:
            data = dict(data)
            data["newPassword"] = data.pop("password")
        if isinstance(data, dict) and "newPassword" in data and "new_password" not in data:
            data = dict(data)
            data["new_password"] = data.pop("newPassword")
        return data


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=240)
    phone: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=160)
    avatar_url: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "fullName" in data and "full_name" not in data:
                data["full_name"] = data.pop("fullName")
            if "avatarUrl" in data and "avatar_url" not in data:
                data["avatar_url"] = data.pop("avatarUrl")
        return data

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return normalize_phone_number(value)

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("invalid_avatar_url")
        return value.strip()


class EmailVerificationConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32, max_length=512)


class EmailVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    captcha_token: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case(cls, data: Any) -> Any:
        if isinstance(data, dict) and "captchaToken" in data and "captcha_token" not in data:
            data = dict(data)
            data["captcha_token"] = data.pop("captchaToken")
        return data


class PhoneOtpSendRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    phone: str = Field(min_length=8, max_length=32)
    purpose: str = Field(default="phone_verification", max_length=64)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        normalized = normalize_phone_number(value)
        if normalized is None:
            raise ValueError("invalid phone number")
        return normalized


class PhoneOtpVerifyRequest(PhoneOtpSendRequest):
    otp: str = Field(min_length=4, max_length=12)
