from __future__ import annotations

import uuid
import asyncio
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import (
    AccountSecurity,
    LoginAttempt,
    PasswordResetTokenState,
    PhoneOtpToken,
    Profile,
    RefreshToken,
    RefreshTokenSecurity,
    User,
    UserRole,
    VerificationToken,
)
from ..repositories.resources import serialize_record
from ..security.passwords import hash_password, validate_password, verify_password
from ..security.tokens import create_access_token, create_refresh_token, token_hash
from .api_protection import trusted_client_ip

ACTIVE_ACCOUNT_STATUS = "active"
LOGIN_BLOCKED_STATUSES = {
    "pending_email_verification",
    "pending_merchant_review",
    "disabled",
    "merchant_rejected",
    "deletion_pending",
    "deleted",
    "anonymized",
}
LOGIN_AUXILIARY_DB_TIMEOUT_SECONDS = 2.5
PASSWORD_VERIFY_TIMEOUT_SECONDS = 12.0
KNOWN_AUTH_ROLES = {
    "customer",
    "admin",
    "manager",
    "finance",
    "partner",
    "courier",
    "delivery",
    "logistics",
    "marketer",
    "staff",
    "employee",
}
ROLE_ALIASES = {
    "administrator": "admin",
    "super_admin": "admin",
    "superadmin": "admin",
    "staff_admin": "admin",
    "store_admin": "admin",
    "owner": "admin",
    "ادمن": "admin",
    "أدمن": "admin",
    "مشرف_عام": "admin",
    "مشرف عام": "admin",
    "إدارة": "admin",
    "ادارة": "admin",
    "supervisor": "manager",
    "staff_supervisor": "manager",
    "مدير": "manager",
    "مشرف": "manager",
    "accountant": "finance",
    "محاسب": "finance",
    "مالية": "finance",
    "shipping": "logistics",
    "لوجستيات": "logistics",
    "شحن": "logistics",
    "driver": "courier",
    "delivery_driver": "courier",
    "courier_driver": "courier",
    "delivery_agent": "courier",
    "مندوب": "courier",
    "موصل": "courier",
    "مندوب_توصيل": "courier",
    "مندوب توصيل": "courier",
    "موصل_طلبات": "courier",
    "موصل طلبات": "courier",
    "موظف": "employee",
    "vendor": "partner",
    "merchant": "partner",
    "تاجر": "partner",
    "affiliate": "marketer",
    "مسوق": "marketer",
    "client": "customer",
    "عميل": "customer",
    "زبون": "customer",
}
PROFILE_ROLE_KEYS = (
    "role",
    "roles",
    "user_role",
    "user_roles",
    "account_role",
    "account_roles",
    "app_role",
    "app_roles",
    "type",
    "account_type",
    "classification",
)


def _bootstrap_admin_emails() -> set[str]:
    raw = getattr(get_settings(), "bootstrap_admin_emails", "") or ""
    return {
        item.strip().lower()
        for item in raw.replace(";", ",").replace("|", ",").split(",")
        if item.strip()
    }


async def _safe_execute(
    session: AsyncSession,
    statement: Any,
    *,
    timeout: float = LOGIN_AUXILIARY_DB_TIMEOUT_SECONDS,
):
    try:
        return await asyncio.wait_for(session.execute(statement), timeout=timeout)
    except (asyncio.TimeoutError, SQLAlchemyError):
        await session.rollback()
        return None


async def _safe_flush(
    session: AsyncSession,
    *,
    timeout: float = LOGIN_AUXILIARY_DB_TIMEOUT_SECONDS,
) -> bool:
    try:
        await asyncio.wait_for(session.flush(), timeout=timeout)
        return True
    except (asyncio.TimeoutError, SQLAlchemyError):
        await session.rollback()
        return False


async def _table_exists(session: AsyncSession, table_name: str) -> bool:
    if not isinstance(session, AsyncSession):
        return True
    result = await session.execute(select(func.to_regclass(f"public.{table_name}")))
    return result.scalar_one_or_none() is not None


async def _optional_table_ready(session: AsyncSession, table_name: str) -> bool:
    try:
        return await _table_exists(session, table_name)
    except SQLAlchemyError:
        await session.rollback()
        return False


def _canonical_role(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.lower().replace("-", "_")
    normalized = "_".join(normalized.split())
    if normalized in KNOWN_AUTH_ROLES:
        return normalized
    return ROLE_ALIASES.get(normalized) or ROLE_ALIASES.get(text.strip().lower())


def _roles_from_any(value: Any) -> set[str]:
    roles: set[str] = set()
    if value is None:
        return roles
    if isinstance(value, dict):
        for key in PROFILE_ROLE_KEYS:
            roles.update(_roles_from_any(value.get(key)))
        boolean_aliases = {
            "is_admin": "admin",
            "isAdmin": "admin",
            "admin": "admin",
            "is_manager": "manager",
            "isManager": "manager",
            "manager": "manager",
            "is_merchant": "partner",
            "isMerchant": "partner",
            "merchant": "partner",
            "is_partner": "partner",
            "isPartner": "partner",
            "partner": "partner",
            "is_courier": "courier",
            "isCourier": "courier",
            "courier": "courier",
            "is_marketer": "marketer",
            "isMarketer": "marketer",
            "marketer": "marketer",
        }
        for key, role in boolean_aliases.items():
            if value.get(key) is True:
                roles.add(role)
        return roles
    if isinstance(value, (list, tuple, set)):
        for item in value:
            roles.update(_roles_from_any(item))
        return roles
    text = str(value).strip()
    if any(separator in text for separator in (",", ";", "|")):
        for part in text.replace(";", ",").replace("|", ",").split(","):
            role = _canonical_role(part)
            if role:
                roles.add(role)
        return roles
    role = _canonical_role(text)
    if role:
        roles.add(role)
    return roles


async def _profile_roles_for(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    try:
        result = await session.execute(select(Profile).where(Profile.user_id == user_id))
    except SQLAlchemyError:
        await session.rollback()
        return []
    profile = result.scalar_one_or_none()
    if profile is None:
        return []
    roles = _roles_from_any(profile.classification)
    roles.update(_roles_from_any(profile.extra_data))
    return sorted(roles)


async def _bootstrap_roles_for(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    admin_emails = _bootstrap_admin_emails()
    if not admin_emails:
        return []
    try:
        result = await session.execute(select(User.email).where(User.id == user_id))
    except SQLAlchemyError:
        await session.rollback()
        return []
    email = (result.scalar_one_or_none() or "").strip().lower()
    if email and email in admin_emails:
        return ["admin"]
    return []


async def roles_for(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    result = await session.execute(
        select(UserRole.role).where(UserRole.user_id == user_id).order_by(UserRole.role)
    )
    roles = sorted({role for item in result.scalars() if (role := _canonical_role(item))})
    if roles:
        return roles
    roles = await _profile_roles_for(session, user_id)
    if roles:
        return roles
    return await _bootstrap_roles_for(session, user_id)




async def account_security_for(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    create: bool = True,
    for_update: bool = False,
) -> AccountSecurity:
    if not await _optional_table_ready(session, AccountSecurity.__tablename__):
        return AccountSecurity(user_id=user_id, account_status=ACTIVE_ACCOUNT_STATUS, security_version=0)
    statement = select(AccountSecurity).where(AccountSecurity.user_id == user_id)
    if for_update:
        statement = statement.with_for_update()
    try:
        result = await session.execute(statement)
    except SQLAlchemyError:
        await session.rollback()
        return AccountSecurity(user_id=user_id, account_status=ACTIVE_ACCOUNT_STATUS, security_version=0)
    state = result.scalar_one_or_none()
    if state is None:
        if not create:
            return AccountSecurity(user_id=user_id, account_status=ACTIVE_ACCOUNT_STATUS, security_version=0)
        state = AccountSecurity(user_id=user_id, account_status=ACTIVE_ACCOUNT_STATUS, security_version=0)
        session.add(state)
        try:
            await session.flush()
        except SQLAlchemyError:
            await session.rollback()
            return AccountSecurity(user_id=user_id, account_status=ACTIVE_ACCOUNT_STATUS, security_version=0)
    return state


def account_can_login(user: User, state: AccountSecurity) -> bool:
    return (
        user.is_active is True
        and user.deleted_at is None
        and state.account_status == ACTIVE_ACCOUNT_STATUS
    )


def extract_client_ip(request: Request | None) -> str:
    if request is None or request.client is None:
        return "unknown"
    peer = request.client.host or "unknown"
    trusted_proxy_set = getattr(get_settings(), "trusted_proxy_set", set()) or set()
    if trusted_proxy_set:
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        trusted = False
        for item in trusted_proxy_set:
            try:
                if peer_address in ipaddress.ip_network(str(item), strict=False):
                    trusted = True
                    break
            except ValueError:
                if peer == str(item):
                    trusted = True
                    break
        if not trusted:
            return peer
        forwarded = request.headers.get("x-forwarded-for", "")
        for raw in forwarded.split(","):
            candidate = raw.strip().strip('"')
            if not candidate:
                continue
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            return candidate
    return trusted_client_ip(request)


async def record_security_event(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    event_type: str,
    status: str = "recorded",
    description: str = "",
    request: Request | None = None,
) -> None:
    model = MODEL_BY_TABLE.get("security_events")
    if model is None:
        return
    if not await _optional_table_ready(session, "security_events"):
        return
    session.add(
        model(
            user_id=user_id,
            type=event_type,
            status=status,
            description=description[:1000],
            path=str(getattr(getattr(request, "url", None), "path", "")) if request is not None else None,
        )
    )
    try:
        await session.flush()
    except SQLAlchemyError:
        await session.rollback()


async def auth_payload(
    session: AsyncSession,
    user: User,
    request: Request | None = None,
    issue_tokens: bool = True,
    session_family_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    roles = await roles_for(session, user.id)
    account_state = await account_security_for(session, user.id)
    profile_result = await session.execute(select(Profile).where(Profile.user_id == user.id))
    profile = profile_result.scalar_one_or_none()
    payload: dict[str, Any] = {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "is_active": user.is_active,
            "role": roles[0] if roles else None,
            "roles": roles,
            "account_status": account_state.account_status,
            "email_verified_at": _iso_or_none(account_state.email_verified_at),
            "phone_verified_at": _iso_or_none(account_state.phone_verified_at),
            "security_version": int(account_state.security_version or 0),
        },
        "profile": serialize_record(profile) if profile else None,
        "roles": roles,
    }
    if issue_tokens:
        payload.update({
            "access_token": create_access_token(
                str(user.id),
                roles,
                security_version=int(account_state.security_version or 0),
            ),
            "token_type": "bearer",
            "expires_in": get_settings().jwt_access_token_minutes * 60,
        })
        if await _optional_table_ready(session, RefreshToken.__tablename__):
            raw_refresh, refresh_hash, expires_at = create_refresh_token()
            family_id = session_family_id or uuid.uuid4()
            refresh = RefreshToken(
                user_id=user.id,
                token_hash=refresh_hash,
                expires_at=expires_at,
                user_agent=request.headers.get("user-agent") if request else None,
                ip_address=extract_client_ip(request),
            )
            session.add(refresh)
            if not await _safe_flush(session):
                return payload
            if await _optional_table_ready(session, RefreshTokenSecurity.__tablename__):
                session.add(RefreshTokenSecurity(refresh_token_id=refresh.id, session_family_id=family_id))
                if not await _safe_flush(session):
                    return payload
            payload["refresh_token"] = raw_refresh
    return payload


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def check_login_rate_limit(session: AsyncSession, email: str, ip: str) -> None:
    if not await _optional_table_ready(session, LoginAttempt.__tablename__):
        return
    since = datetime.now(timezone.utc) - timedelta(minutes=15)
    result = await _safe_execute(
        session,
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.email == email,
            LoginAttempt.ip_address == ip,
            LoginAttempt.succeeded.is_(False),
            LoginAttempt.created_at >= since,
        )
    )
    if result is None:
        return
    if int(result.scalar_one()) >= get_settings().login_rate_limit:
        raise HTTPException(status_code=429, detail="too_many_login_attempts")


async def check_action_rate_limit(
    session: AsyncSession,
    *,
    email: str,
    ip: str,
    detail: str,
    maximum: int,
    window_minutes: int = 30,
) -> None:
    if not await _optional_table_ready(session, LoginAttempt.__tablename__):
        return
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    result = await _safe_execute(
        session,
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.detail == detail,
            LoginAttempt.created_at >= since,
            or_(LoginAttempt.email == email, LoginAttempt.ip_address == ip),
        )
    )
    if result is None:
        return
    if int(result.scalar_one()) >= maximum:
        raise HTTPException(status_code=429, detail=f"too_many_{detail}_attempts")


async def record_login_attempt(
    session: AsyncSession, email: str, ip: str, succeeded: bool, detail: str | None = None
) -> None:
    if not await _optional_table_ready(session, LoginAttempt.__tablename__):
        return
    session.add(LoginAttempt(email=email, ip_address=ip, succeeded=succeeded, detail=detail))
    await _safe_flush(session)


async def authenticate(session: AsyncSession, email: str, password: str, ip: str) -> User:
    normalized = email.strip().lower()
    await check_login_rate_limit(session, normalized, ip)
    result = await session.execute(select(User).where(func.lower(User.email) == normalized))
    user = result.scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        await record_login_attempt(session, normalized, ip, False, "unknown_or_inactive_user")
        raise HTTPException(status_code=401, detail="invalid_login")
    account_state = await account_security_for(session, user.id)
    if account_state.account_status != ACTIVE_ACCOUNT_STATUS:
        await record_login_attempt(session, normalized, ip, False, "account_not_active")
        raise HTTPException(status_code=403, detail=account_state.account_status or "account_not_active")
    if not user.is_active:
        await record_login_attempt(session, normalized, ip, False, "unknown_or_inactive_user")
        raise HTTPException(status_code=401, detail="invalid_login")
    try:
        valid, needs_rehash = await asyncio.wait_for(
            asyncio.to_thread(verify_password, password, user.password_hash, user.password_salt),
            timeout=PASSWORD_VERIFY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as error:
        await session.rollback()
        raise HTTPException(status_code=503, detail="auth_temporarily_unavailable") from error
    if not valid:
        await record_login_attempt(session, normalized, ip, False, "bad_password")
        raise HTTPException(status_code=401, detail="invalid_login")
    if needs_rehash:
        user.password_hash = await asyncio.to_thread(hash_password, password)
        user.password_salt = None
        user.password_must_reset = False
    user.last_login_at = datetime.now(timezone.utc)
    await record_login_attempt(session, normalized, ip, True)
    return user


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str,
    phone: str | None,
    city: str | None,
    extra_data: dict[str, Any] | None = None,
    role: str | None = "customer",
    account_status: str = ACTIVE_ACCOUNT_STATUS,
    is_active: bool = True,
) -> User:
    normalized = email.strip().lower()
    validate_password(password, email=normalized)
    existing = await session.execute(select(User.id).where(func.lower(User.email) == normalized))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="email_exists")
    password_hash = await asyncio.to_thread(hash_password, password)
    user = User(
        email=normalized,
        password_hash=password_hash,
        is_active=is_active,
    )
    try:
        session.add(user)
        await session.flush()
        session.add(Profile(
            id=user.id,
            user_id=user.id,
            email=normalized,
            full_name=full_name.strip(),
            phone=phone,
            city=city,
            extra_data=dict(extra_data or {}),
        ))
        if role:
            session.add(UserRole(user_id=user.id, role=role))
        session.add(
            AccountSecurity(
                user_id=user.id,
                account_status=account_status,
                security_version=0,
            )
        )
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(status_code=409, detail="email_exists") from error
    return user


async def revoke_all_refresh_tokens(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> int:
    if not await _optional_table_ready(session, RefreshToken.__tablename__):
        return 0
    timestamp = now or datetime.now(timezone.utc)
    try:
        result = await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=timestamp)
        )
        await session.flush()
    except SQLAlchemyError:
        await session.rollback()
        return 0
    return int(getattr(result, "rowcount", 0) or 0)


async def cleanup_security_artifacts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    login_attempt_retention_days: int = 30,
) -> dict[str, int]:
    timestamp = now or datetime.now(timezone.utc)
    blocked_users = (
        select(User.id)
        .outerjoin(AccountSecurity, AccountSecurity.user_id == User.id)
        .where(
            or_(
                User.is_active.is_not(True),
                User.deleted_at.is_not(None),
                AccountSecurity.account_status.is_not(None) & (AccountSecurity.account_status != ACTIVE_ACCOUNT_STATUS),
            )
        )
    )
    revoked = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > timestamp,
            RefreshToken.user_id.in_(blocked_users),
        )
        .values(revoked_at=timestamp)
    )
    expired_verifications = await session.execute(
        update(VerificationToken)
        .where(
            VerificationToken.used_at.is_(None),
            VerificationToken.invalidated_at.is_(None),
            VerificationToken.expires_at <= timestamp,
        )
        .values(invalidated_at=timestamp)
    )
    expired_otps = await session.execute(
        update(PhoneOtpToken)
        .where(
            PhoneOtpToken.used_at.is_(None),
            PhoneOtpToken.invalidated_at.is_(None),
            PhoneOtpToken.expires_at <= timestamp,
        )
        .values(invalidated_at=timestamp)
    )
    old_login_attempts = await session.execute(
        delete(LoginAttempt).where(LoginAttempt.created_at < timestamp - timedelta(days=login_attempt_retention_days))
    )
    await session.flush()
    return {
        "revoked_refresh_tokens": int(getattr(revoked, "rowcount", 0) or 0),
        "invalidated_verification_tokens": int(getattr(expired_verifications, "rowcount", 0) or 0),
        "invalidated_phone_otps": int(getattr(expired_otps, "rowcount", 0) or 0),
        "deleted_old_login_attempts": int(getattr(old_login_attempts, "rowcount", 0) or 0),
    }


async def revoke_refresh_family(
    session: AsyncSession,
    stored: RefreshToken,
    *,
    now: datetime | None = None,
) -> int:
    if not await _optional_table_ready(session, RefreshToken.__tablename__):
        return 0
    timestamp = now or datetime.now(timezone.utc)
    security = None
    if await _optional_table_ready(session, RefreshTokenSecurity.__tablename__):
        try:
            security = await session.get(RefreshTokenSecurity, stored.id)
        except SQLAlchemyError:
            await session.rollback()
            security = None
    if not isinstance(security, RefreshTokenSecurity):
        security = None
    family_id = security.session_family_id if security is not None else stored.id
    if security is None:
        token_filter = RefreshToken.id == stored.id
    else:
        family_tokens = select(RefreshTokenSecurity.refresh_token_id).where(
            RefreshTokenSecurity.session_family_id == family_id
        )
        token_filter = RefreshToken.id.in_(family_tokens)
    try:
        result = await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == stored.user_id,
                token_filter,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=timestamp)
        )
        await session.flush()
    except SQLAlchemyError:
        await session.rollback()
        return 0
    return int(getattr(result, "rowcount", 0) or 0)


async def bump_security_version(
    session: AsyncSession,
    user: User,
    *,
    reason: str,
    request: Request | None = None,
) -> None:
    account_state = await account_security_for(session, user.id, for_update=True)
    account_state.security_version = int(account_state.security_version or 0) + 1
    await record_security_event(
        session,
        user_id=user.id,
        event_type=reason,
        status="applied",
        description=f"security_version={account_state.security_version}",
        request=request,
    )


async def rotate_refresh_token(
    session: AsyncSession, raw_refresh: str, request: Request
) -> dict[str, Any]:
    digest = token_hash(raw_refresh)
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == digest).with_for_update()
    )
    stored = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if stored is None:
        raise HTTPException(status_code=401, detail="invalid_refresh_token")
    if stored.revoked_at is not None:
        token_state = None
        if await _optional_table_ready(session, RefreshTokenSecurity.__tablename__):
            try:
                token_state = await session.get(RefreshTokenSecurity, stored.id)
            except SQLAlchemyError:
                await session.rollback()
                token_state = None
            if not isinstance(token_state, RefreshTokenSecurity):
                token_state = None
        if token_state is None:
            if await _optional_table_ready(session, RefreshTokenSecurity.__tablename__):
                token_state = RefreshTokenSecurity(refresh_token_id=stored.id, session_family_id=stored.id)
                session.add(token_state)
        if token_state is not None:
            token_state.reused_at = token_state.reused_at or now
        await revoke_refresh_family(session, stored, now=now)
        await record_security_event(
            session,
            user_id=stored.user_id,
            event_type="refresh_token_reuse_detected",
            status="blocked",
            description="Revoked refresh token was reused; family revoked.",
            request=request,
        )
        raise HTTPException(status_code=401, detail="refresh_token_reuse_detected")
    if stored.expires_at <= now:
        stored.revoked_at = now
        raise HTTPException(status_code=401, detail="invalid_refresh_token")
    user = await session.get(User, stored.user_id)
    account_state = await account_security_for(session, stored.user_id)
    if user is None or not account_can_login(user, account_state):
        await revoke_refresh_family(session, stored, now=now)
        raise HTTPException(status_code=401, detail="inactive_user")
    stored.revoked_at = now
    stored_state = None
    if await _optional_table_ready(session, RefreshTokenSecurity.__tablename__):
        try:
            stored_state = await session.get(RefreshTokenSecurity, stored.id)
        except SQLAlchemyError:
            await session.rollback()
            stored_state = None
        if not isinstance(stored_state, RefreshTokenSecurity):
            stored_state = None
    family_id = stored_state.session_family_id if stored_state is not None else stored.id
    payload = await auth_payload(
        session,
        user,
        request=request,
        issue_tokens=True,
        session_family_id=family_id,
    )
    if payload.get("refresh_token"):
        new_hash = token_hash(str(payload["refresh_token"]))
        replacement = await session.execute(
            select(RefreshToken.id).where(RefreshToken.token_hash == new_hash)
        )
        stored.replaced_by_id = replacement.scalar_one_or_none()
    await record_security_event(
        session,
        user_id=user.id,
        event_type="refresh_token_rotated",
        status="success",
        description="Refresh token rotated.",
        request=request,
    )
    return payload


async def revoke_refresh_token(session: AsyncSession, raw_refresh: str) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash(raw_refresh), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )


async def create_verification_token(
    session: AsyncSession,
    *,
    user: User,
    purpose: str,
    target: str,
    request: Request | None = None,
    expires_minutes: int = 60,
) -> str:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(VerificationToken)
        .where(
            VerificationToken.user_id == user.id,
            VerificationToken.purpose == purpose,
            VerificationToken.used_at.is_(None),
            VerificationToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    raw_token = secrets.token_urlsafe(48)
    session.add(
        VerificationToken(
            user_id=user.id,
            purpose=purpose,
            target=target,
            token_hash=token_hash(raw_token),
            expires_at=now + timedelta(minutes=expires_minutes),
            requested_ip=extract_client_ip(request),
        )
    )
    await session.flush()
    return raw_token


async def create_email_verification_code(
    session: AsyncSession,
    *,
    user: User,
    target: str,
    request: Request | None = None,
    expires_minutes: int = 10,
) -> str:
    """Create a short-lived email code without storing the code itself.

    The user id is included in the digest so the same six-digit value can be
    safely active for different accounts without colliding on the unique
    token_hash column.
    """
    now = datetime.now(timezone.utc)
    await session.execute(
        update(VerificationToken)
        .where(
            VerificationToken.user_id == user.id,
            VerificationToken.purpose == "email_verification",
            VerificationToken.used_at.is_(None),
            VerificationToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(
        VerificationToken(
            user_id=user.id,
            purpose="email_verification",
            target=target,
            token_hash=token_hash(f"{user.id}:{code}"),
            expires_at=now + timedelta(minutes=expires_minutes),
            requested_ip=extract_client_ip(request),
        )
    )
    await session.flush()
    return code


async def create_phone_otp_token(
    session: AsyncSession,
    *,
    user: User,
    phone: str,
    purpose: str,
    request: Request | None = None,
    expires_minutes: int = 10,
) -> str:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(PhoneOtpToken)
        .where(
            PhoneOtpToken.user_id == user.id,
            PhoneOtpToken.phone == phone,
            PhoneOtpToken.purpose == purpose,
            PhoneOtpToken.used_at.is_(None),
            PhoneOtpToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(
        PhoneOtpToken(
            user_id=user.id,
            phone=phone,
            purpose=purpose,
            otp_hash=token_hash(code),
            expires_at=now + timedelta(minutes=expires_minutes),
            requested_ip=extract_client_ip(request),
        )
    )
    await session.flush()
    return code
