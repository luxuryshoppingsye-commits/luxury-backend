from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)


@dataclass(frozen=True)
class PasswordPolicy:
    min_length: int = 8
    max_length: int = 256
    require_letter: bool = True
    require_number: bool = True
    require_symbol: bool = False
    reject_edge_whitespace: bool = True
    reject_email_match: bool = True
    unicode_normalization: str = "preserve"
    public_message_ar: str = "كلمة المرور يجب أن تكون 8 أحرف على الأقل وتحتوي على حرف ورقم."
    public_message_en: str = "Password must be at least 8 characters and include a letter and a number."

    def public_dict(self) -> dict[str, object]:
        return asdict(self)


PASSWORD_POLICY = PasswordPolicy()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, encoded: str, legacy_salt: str | None = None) -> tuple[bool, bool]:
    if encoded.startswith("$argon2id$"):
        try:
            valid = _hasher.verify(encoded, password)
            return valid, valid and _hasher.check_needs_rehash(encoded)
        except (VerifyMismatchError, InvalidHashError, VerificationError):
            return False, False
    if not legacy_salt:
        return False, False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), legacy_salt.encode("utf-8"), 120_000
    ).hex()
    return hmac.compare_digest(candidate, encoded), True


def get_password_policy() -> PasswordPolicy:
    return PASSWORD_POLICY


def validate_password(password: str, *, email: str | None = None) -> None:
    policy = get_password_policy()
    if policy.reject_edge_whitespace and password != password.strip():
        raise ValueError("password_policy_violation")
    if len(password) < policy.min_length:
        raise ValueError("password_too_short")
    if len(password) > policy.max_length:
        raise ValueError("password_too_long")
    if policy.require_letter and not any(ch.isalpha() for ch in password):
        raise ValueError("password_requires_letters_and_numbers")
    if policy.require_number and not any(ch.isdigit() for ch in password):
        raise ValueError("password_requires_letters_and_numbers")
    if policy.require_symbol and not any(not ch.isalnum() for ch in password):
        raise ValueError("password_policy_violation")
    if policy.reject_email_match and email and password.casefold() == email.casefold():
        raise ValueError("password_policy_violation")
