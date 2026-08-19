from __future__ import annotations

import uuid
import secrets
import asyncio
import logging
from datetime import timedelta
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...dependencies import current_user, require_admin, user_roles
from ...models import MODEL_BY_TABLE
from ...config import get_settings
from ...models.domain import LoginAttempt, PasswordResetToken, PasswordResetTokenState, PhoneOtpToken, Profile, RefreshToken, RefreshTokenSecurity, User, UserRole, VerificationToken
from ...models.domain import StaffPermissionSet
from ...repositories.resources import serialize_record
from ...schemas.auth import (
    EmailVerificationConfirm,
    EmailVerificationRequest,
    FirebaseAuthRequest,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    PasswordResetConfirm,
    PhoneOtpSendRequest,
    PhoneOtpVerifyRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
)
from ...security.passwords import get_password_policy, hash_password, validate_password, verify_password
from ...security.tokens import token_hash
from ...services.auth_service import (
    auth_payload,
    account_security_for,
    authenticate,
    bump_security_version,
    check_action_rate_limit,
    cleanup_security_artifacts,
    create_phone_otp_token,
    create_user,
    create_email_verification_code,
    extract_client_ip,
    record_login_attempt,
    record_security_event,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
)
from ...services.api_protection import capabilities_for_roles
from ...services.staff_permissions import (
    ALL_PERMISSIONS,
    PERMISSION_GROUPS,
    effective_permissions,
    normalize_permissions,
)
from ...services.firebase_auth_service import firebase_admin_configuration_status, verify_firebase_id_token
from ...services.outbox_service import deliver_email_now, email_delivery_configured


router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)
PartnerApplication = MODEL_BY_TABLE["partner_applications"]
AccountDeletionRequest = MODEL_BY_TABLE["account_deletion_requests"]
EMAIL_OUTBOX = MODEL_BY_TABLE["email_outbox"]
WHATSAPP_OUTBOX = MODEL_BY_TABLE["whatsapp_outbox"]


async def _session_after_json(request: Request):
    """Parse request JSON before opening a database session.

    This keeps malformed client payloads as deterministic 400 responses even
    when PostgreSQL is temporarily unavailable.
    """
    await request.json()
    async for session in get_session():
        yield session


def _append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[key] = value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _normalized_url_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    if parsed.scheme:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return ""


def _is_local_url(value: str) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _resolve_password_reset_redirect(redirect_to: str | None, client_type: str | None) -> str:
    settings = get_settings()
    if not redirect_to or not redirect_to.strip():
        if client_type:
            normalized_client = client_type.strip().lower()
            if normalized_client in {"flutter", "mobile", "android", "ios"}:
                return settings.flutter_reset_deep_link
            if normalized_client in {"web", "website", "react"}:
                return f"{settings.frontend_public_url.rstrip('/')}/reset-password"
        return f"{settings.frontend_public_url.rstrip('/')}/reset-password"
    candidate = redirect_to.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() in {"javascript", "data", "file"} or candidate.startswith("//"):
        raise HTTPException(status_code=422, detail="invalid_redirect_url")
    if settings.app_env == "production" and _is_local_url(candidate):
        raise HTTPException(status_code=422, detail="invalid_redirect_url")
    allowed = settings.password_reset_redirect_allowlist
    candidate_origin = _normalized_url_origin(candidate)
    for entry in allowed:
        allowed_value = entry.rstrip("/")
        allowed_origin = _normalized_url_origin(allowed_value)
        if parsed.scheme in {"http", "https"} and candidate_origin and candidate_origin == allowed_origin:
            return candidate
        if parsed.scheme and not parsed.netloc and candidate.startswith(allowed_value):
            return candidate
        if candidate.rstrip("/") == allowed_value:
            return candidate
    raise HTTPException(status_code=422, detail="invalid_redirect_url")


@router.get("/auth/password-policy")
@router.get("/api/auth/password-policy")
async def password_policy():
    return {"data": get_password_policy().public_dict()}


def _repair_mojibake(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not any(marker in value for marker in ("\u00d8", "\u00d9", "\u00c3", "\u00c2", "\ufffd")):
        return value
    try:
        repaired = value.encode("latin1", errors="strict").decode("utf-8")
    except UnicodeError:
        return value
    return repaired if repaired else value


def _smtp_is_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.smtp_host
        and settings.smtp_username
        and settings.smtp_password
        and (settings.smtp_from_email or settings.smtp_username)
    )


def _email_delivery_status() -> str:
    settings = get_settings()
    if settings.fixtures_enabled:
        return "queued_test_provider"
    if email_delivery_configured(settings):
        return "pending"
    return "blocked_credentials"


def _phone_delivery_status() -> str:
    settings = get_settings()
    if settings.fixtures_enabled:
        return "queued_test_provider"
    if settings.whatsapp_provider_url and settings.whatsapp_access_token:
        return "pending"
    return "blocked_credentials"


async def _deliver_phone_otp_or_raise(phone: str, code: str) -> tuple[str, dict[str, Any]]:
    settings = get_settings()
    if settings.fixtures_enabled:
        return "queued_test_provider", {"provider": "test"}
    if not settings.whatsapp_provider_url or not settings.whatsapp_access_token:
        raise HTTPException(status_code=503, detail="phone_provider_unconfigured")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                settings.whatsapp_provider_url,
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                json={
                    "to": phone,
                    "title": "Phone verification",
                    "message": f"Your verification code is {code}. It expires in 10 minutes.",
                },
            )
            response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail="phone_provider_failed") from error
    return "sent", {"provider_status_code": response.status_code}


async def _assert_registration_allowed(
    session: AsyncSession,
    request: Request,
    *,
    email: str,
    captcha_token: str | None,
) -> str:
    settings = get_settings()
    ip = extract_client_ip(request)
    await check_action_rate_limit(
        session,
        email=email,
        ip=ip,
        detail="registration",
        maximum=settings.registration_rate_limit,
        window_minutes=30,
    )
    session.add(LoginAttempt(email=email, ip_address=ip, succeeded=True, detail="registration"))
    if not settings.captcha_required:
        return "not_required"
    if settings.fixtures_enabled and captcha_token == "test-captcha-ok":
        return "verified_test_provider"
    if not captcha_token:
        await record_security_event(
            session,
            user_id=None,
            event_type="captcha_missing",
            status="blocked",
            description="Registration CAPTCHA is required but no token was provided.",
            request=request,
        )
        raise HTTPException(status_code=400, detail="captcha_required")
    if not settings.captcha_secret:
        await record_security_event(
            session,
            user_id=None,
            event_type="captcha_provider_unconfigured",
            status="blocked_credentials",
            description="Registration CAPTCHA is required but no provider secret is configured.",
            request=request,
        )
        raise HTTPException(status_code=503, detail="captcha_provider_unconfigured")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                settings.captcha_verify_url,
                data={"secret": settings.captcha_secret, "response": captcha_token, "remoteip": ip},
            )
            response.raise_for_status()
            verification = response.json()
    except (httpx.HTTPError, ValueError) as error:
        await record_security_event(
            session,
            user_id=None,
            event_type="captcha_provider_error",
            status="failed",
            description="Registration CAPTCHA provider verification failed.",
            request=request,
        )
        raise HTTPException(status_code=503, detail="captcha_provider_failed") from error
    if verification.get("success") is not True:
        await record_security_event(
            session,
            user_id=None,
            event_type="captcha_failed",
            status="blocked",
            description="Registration CAPTCHA token was rejected by provider.",
            request=request,
        )
        raise HTTPException(status_code=400, detail="captcha_failed")
    return "verified"


async def _queue_email_verification(
    session: AsyncSession,
    *,
    user: User,
    request: Request,
) -> dict[str, Any]:
    code = await create_email_verification_code(
        session,
        user=user,
        target=user.email,
        request=request,
        expires_minutes=10,
    )
    settings = get_settings()
    verify_link = (
        f"{settings.frontend_public_url.rstrip('/')}/verify-email?"
        f"{urlencode({'email': user.email, 'code': code})}"
    )
    status = _email_delivery_status()
    session.add(
        EMAIL_OUTBOX(
            user_id=user.id,
            title="فعّل حسابك في رفاهية التسوق",
            email=user.email,
            message=(
                "مرحبًا بك في رفاهية التسوق. رمز تفعيل حسابك هو: "
                f"{code}. ينتهي الرمز خلال 10 دقائق. يمكنك أيضًا الضغط على زر تفعيل الحساب."
            ),
            status=status,
            extra_data={
                "purpose": "email_verification",
                "verification_url": verify_link,
                "action_label": "تفعيل الحساب",
                "verification_expires_minutes": 10,
            },
        )
    )
    await record_security_event(
        session,
        user_id=user.id,
        event_type="email_verification_requested",
        status=status,
        description="Email verification code generated.",
        request=request,
    )
    return {
        "requires_verification": True,
        "delivery_status": status,
        "verification_expires_minutes": 10,
    }


@router.post("/auth/login")
async def login(body: LoginRequest, request: Request, session: AsyncSession = Depends(get_session)):
    try:
        user = await authenticate(
            session,
            str(body.email),
            body.password,
            extract_client_ip(request),
        )
    except HTTPException:
        await session.commit()
        raise
    payload = await auth_payload(session, user, request=request)
    await session.commit()
    return payload


def _web_auth_payload(payload: dict[str, Any]) -> dict[str, Any]:
    session_payload = None
    if payload.get("access_token"):
        session_payload = {
            "access_token": payload.get("access_token"),
            "token_type": payload.get("token_type", "bearer"),
            "expires_in": payload.get("expires_in"),
        }
    return {
        "user": {
            **dict(payload.get("user") or {}),
            "roles": payload.get("roles") or [],
            "profile": payload.get("profile"),
        },
        "profile": payload.get("profile"),
        "roles": payload.get("roles") or [],
        "session": session_payload,
        "requires_verification": payload.get("requires_verification", False),
        "delivery_status": payload.get("delivery_status"),
        "captcha_status": payload.get("captcha_status"),
    }


def _set_refresh_cookie(response: Response, payload: dict[str, Any]) -> None:
    secure_cookie = get_settings().app_env in {"production", "staging"}
    access_token = payload.get("access_token")
    if access_token:
        response.set_cookie(
            "at",
            str(access_token),
            httponly=True,
            samesite="lax",
            secure=secure_cookie,
            path="/",
            max_age=int(payload.get("expires_in") or 3600),
        )
    token = payload.get("refresh_token")
    if token:
        response.set_cookie(
            "rt",
            str(token),
            httponly=True,
            samesite="lax",
            secure=secure_cookie,
            path="/",
            max_age=60 * 60 * 24 * 30,
        )


BLOCKED_FIREBASE_ACCOUNT_STATUSES = {"disabled", "deleted", "anonymized", "deletion_pending", "merchant_rejected"}


async def _firebase_auth_payload(
    body: FirebaseAuthRequest,
    request: Request,
    session: AsyncSession,
) -> dict[str, Any]:
    claims = await verify_firebase_id_token(body.id_token)
    # Never bind a Firebase identity to a local account using an unverified
    # email claim. Google/Apple social sign-in must prove ownership of the
    # address before the backend can create or resume a local session.
    if claims.get("email_verified") is not True:
        raise HTTPException(status_code=403, detail="firebase_email_not_verified")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="firebase_email_required")

    firebase_meta = claims.get("firebase") if isinstance(claims.get("firebase"), dict) else {}
    firebase_uid = str(claims.get("uid") or claims.get("sub") or "").strip()
    provider = str(body.provider or firebase_meta.get("sign_in_provider") or "firebase").strip()[:80]
    now = datetime.now(timezone.utc)

    result = await session.execute(select(User).where(func.lower(User.email) == email))
    user = result.scalar_one_or_none()
    if user is not None and user.deleted_at is not None:
        await record_login_attempt(session, email, extract_client_ip(request), False, "firebase_deleted_user")
        raise HTTPException(status_code=403, detail="account_not_active")

    if user is None:
        display_name = str(body.full_name or claims.get("name") or email.split("@", 1)[0]).strip()[:240] or email
        user = await create_user(
            session,
            email=email,
            password=secrets.token_urlsafe(48),
            full_name=display_name,
            phone=body.phone,
            city=body.city,
            role="customer",
            account_status="active",
            is_active=True,
        )

    account_state = await account_security_for(session, user.id, for_update=True)
    if account_state.account_status in BLOCKED_FIREBASE_ACCOUNT_STATUSES:
        await record_login_attempt(session, email, extract_client_ip(request), False, "firebase_account_not_active")
        raise HTTPException(status_code=403, detail=account_state.account_status or "account_not_active")

    account_state.account_status = "active"
    if claims.get("email_verified") is True and account_state.email_verified_at is None:
        account_state.email_verified_at = now
    user.is_active = True
    user.last_login_at = now

    profile_result = await session.execute(select(Profile).where(Profile.user_id == user.id))
    profile = profile_result.scalar_one_or_none()
    if profile is not None:
        if body.full_name and not profile.full_name:
            profile.full_name = body.full_name.strip()
        if body.phone and not profile.phone:
            profile.phone = body.phone
        if body.city and not profile.city:
            profile.city = body.city
        profile.extra_data = {
            **(profile.extra_data or {}),
            "auth_provider": provider,
            "firebase_uid": firebase_uid,
        }

    await record_login_attempt(session, email, extract_client_ip(request), True, "firebase_auth")
    await record_security_event(
        session,
        user_id=user.id,
        event_type="firebase_auth_login",
        status="success",
        description=f"Firebase provider login via {provider}.",
        request=request,
    )
    payload = await auth_payload(session, user, request=request)
    await session.commit()
    return payload


@router.post("/auth/firebase")
async def firebase_auth(body: FirebaseAuthRequest, request: Request, session: AsyncSession = Depends(get_session)):
    return await _firebase_auth_payload(body, request, session)


@router.post("/api/auth/firebase")
async def web_firebase_auth(
    body: FirebaseAuthRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    payload = await _firebase_auth_payload(body, request, session)
    _set_refresh_cookie(response, payload)
    return _web_auth_payload(payload)


@router.post("/api/auth/login")
async def web_login(body: LoginRequest, request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    payload = await login(body, request, session)
    _set_refresh_cookie(response, payload)
    return _web_auth_payload(payload)


@router.post("/auth/register-customer", status_code=201)
async def register_customer(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    normalized_email = str(body.email).strip().lower()
    captcha_status = await _assert_registration_allowed(
        session,
        request,
        email=normalized_email,
        captcha_token=body.captcha_token,
    )
    try:
        user = await create_user(
            session,
            email=normalized_email,
            password=body.password,
            full_name=body.full_name,
            phone=body.phone,
            city=body.city,
            extra_data={
                key: value
                for key, value in {
                    "gender": body.gender,
                    "street": body.street,
                    "address_details": body.address_details,
                }.items()
                if value is not None and str(value).strip()
            },
            role="customer",
            account_status="pending_email_verification",
            is_active=False,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    verification = await _queue_email_verification(session, user=user, request=request)
    payload = await auth_payload(session, user, request=request, issue_tokens=False)
    payload.update(verification)
    payload["captcha_status"] = captcha_status
    await session.commit()
    return payload


@router.post("/api/auth/register", status_code=201)
@router.post("/api/auth/register-customer", status_code=201)
async def web_register_customer(
    body: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    payload = await register_customer(body, request, session)
    _set_refresh_cookie(response, payload)
    return _web_auth_payload(payload)


@router.post("/auth/register-merchant", status_code=201)
async def register_merchant(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    register = RegisterRequest.model_validate({
        "email": body.get("email"),
        "password": body.get("password"),
        "fullName": body.get("ownerName") or body.get("fullName") or body.get("storeName"),
        "phone": body.get("phone"),
        "city": body.get("city"),
        "captchaToken": body.get("captchaToken") or body.get("captcha_token"),
    })
    normalized_email = str(register.email).strip().lower()
    captcha_status = await _assert_registration_allowed(
        session,
        request,
        email=normalized_email,
        captcha_token=register.captcha_token,
    )
    try:
        user = await create_user(
            session,
            email=normalized_email,
            password=register.password,
            full_name=register.full_name,
            phone=register.phone,
            city=register.city,
            role=None,
            account_status="pending_merchant_review",
            is_active=False,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    application = PartnerApplication(
        user_id=user.id,
        name=str(body.get("storeName") or body.get("businessName") or register.full_name),
        email=str(register.email),
        phone=register.phone,
        status="pending",
        description=str(body.get("description") or ""),
        logo_url=body.get("logoUrl"),
        extra_data={
            key: body.get(key)
            for key in (
                "businessType", "city", "commercialRegisterUrl",
                "storeInsideImageUrl", "storeOutsideImageUrl",
            )
            if body.get(key) is not None
        },
    )
    session.add(application)
    await record_security_event(
        session,
        user_id=user.id,
        event_type="merchant_application_submitted",
        status="pending",
        description="Merchant account created in pending review state.",
        request=request,
    )
    payload = await auth_payload(session, user, request=request, issue_tokens=False)
    payload.update({
        "application_status": "pending",
        "merchant_portal_enabled": False,
        "requires_review": True,
        "captcha_status": captcha_status,
    })
    await session.commit()
    return payload


@router.post("/auth/refresh")
async def refresh(
    body: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    try:
        payload = await rotate_refresh_token(session, body.refresh_token, request)
    except HTTPException:
        await session.commit()
        raise
    await session.commit()
    return payload


@router.post("/api/auth/refresh")
async def web_refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    token = request.cookies.get("rt")
    try:
        body = await request.json()
        if isinstance(body, dict):
            token = body.get("refresh_token") or body.get("refreshToken") or token
    except Exception:
        pass
    if not token:
        raise HTTPException(status_code=401, detail="refresh_token_required")
    try:
        payload = await rotate_refresh_token(session, str(token), request)
    except HTTPException:
        await session.commit()
        raise
    await session.commit()
    _set_refresh_cookie(response, payload)
    return _web_auth_payload(payload)


@router.post("/auth/logout")
async def logout(body: RefreshRequest, session: AsyncSession = Depends(get_session)):
    await revoke_refresh_token(session, body.refresh_token)
    await session.commit()
    return {"ok": True}


@router.post("/api/auth/logout")
async def web_logout(request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    token = request.cookies.get("rt")
    try:
        body = await request.json()
        if isinstance(body, dict):
            token = body.get("refresh_token") or body.get("refreshToken") or token
    except Exception:
        pass
    if token:
        await revoke_refresh_token(session, str(token))
        await session.commit()
    result = {"ok": True}
    response.delete_cookie("at", path="/")
    response.delete_cookie("rt", path="/")
    return result


@router.get("/api/auth/sessions")
@router.get("/auth/sessions")
async def list_active_sessions(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(RefreshToken, RefreshTokenSecurity)
            .join(RefreshTokenSecurity, RefreshTokenSecurity.refresh_token_id == RefreshToken.id, isouter=True)
            .where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .order_by(RefreshToken.created_at.desc())
        )
    ).all()
    return {
        "data": [
            {
                "id": str(row.id),
                "session_family_id": str(state.session_family_id if state is not None else row.id),
                "created_at": row.created_at.isoformat(),
                "expires_at": row.expires_at.isoformat(),
                "user_agent": row.user_agent,
                "ip_address": row.ip_address,
            }
            for row, state in rows
        ]
    }


@router.delete("/api/auth/sessions/{session_id}")
@router.delete("/auth/sessions/{session_id}")
async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(RefreshToken, session_id, with_for_update=True)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
    await record_security_event(
        session,
        user_id=user.id,
        event_type="session_revoked",
        status="success",
        description=f"session_id={session_id}",
        request=request,
    )
    await session.commit()
    return {"ok": True}


@router.post("/api/auth/logout-all")
@router.post("/auth/logout-all")
async def logout_all_sessions(
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    revoked = await revoke_all_refresh_tokens(session, user.id)
    await bump_security_version(session, user, reason="logout_all", request=request)
    await session.commit()
    response.delete_cookie("at", path="/")
    response.delete_cookie("rt", path="/")
    return {"ok": True, "revoked": revoked}


@router.post("/api/auth/security/cleanup")
@router.post("/auth/security/cleanup")
async def cleanup_account_security(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await cleanup_security_artifacts(session)
    await session.commit()
    return {"ok": True, **result}


@router.get("/me")
async def me(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    return await auth_payload(session, user, request=request, issue_tokens=False)


@router.get("/api/auth/me")
async def web_me(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    payload = await me(request, user, session)
    return _web_auth_payload(payload)


@router.get("/api/auth/capabilities")
@router.get("/auth/capabilities")
async def auth_capabilities(
    user: User = Depends(current_user),
    roles: set[str] = Depends(user_roles),
    session: AsyncSession = Depends(get_session),
):
    permissions = await effective_permissions(session, user.id, roles)
    return capabilities_for_roles(roles, permissions)


def _profile_alias_payload(payload: dict[str, Any]) -> dict[str, Any]:
    profile = dict(payload.get("profile") or {})
    user = dict(payload.get("user") or {})
    if user:
        profile.setdefault("user_id", user.get("id"))
        profile.setdefault("email", user.get("email"))
    profile["roles"] = payload.get("roles") or []
    return {"data": profile}


@router.get("/api/profile")
@router.get("/api/profile/me")
async def profile_alias(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    payload = await auth_payload(session, user, request=request, issue_tokens=False)
    return _profile_alias_payload(payload)


@router.patch("/me")
async def update_me(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        body = ProfileUpdateRequest.model_validate(await request.json())
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=error.errors(include_context=False)) from error
    result = await session.execute(select(Profile).where(Profile.user_id == user.id).with_for_update())
    profile = result.scalar_one()
    values = body.model_dump(exclude_unset=True, by_alias=False)
    mapping = {"full_name": "full_name", "phone": "phone", "city": "city", "avatar_url": "avatar_url"}
    for source, target in mapping.items():
        if source in values:
            setattr(profile, target, _repair_mojibake(values[source]))
    await session.commit()
    return await auth_payload(session, user, request=request, issue_tokens=False)


@router.patch("/api/profile/me")
async def update_profile_alias(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    payload = await update_me(request, user, session)
    return _profile_alias_payload(payload)


@router.post("/me/password")
@router.patch("/me/password")
@router.patch("/api/auth/change-password")
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    valid, _ = await asyncio.to_thread(verify_password, body.current_password, user.password_hash, user.password_salt)
    if not valid:
        raise HTTPException(status_code=401, detail="current_password_invalid")
    try:
        validate_password(body.new_password, email=user.email)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    same_password, _ = await asyncio.to_thread(verify_password, body.new_password, user.password_hash, user.password_salt)
    if same_password:
        raise HTTPException(status_code=409, detail="new_password_same_as_current")
    user.password_hash = await asyncio.to_thread(hash_password, body.new_password)
    user.password_salt = None
    user.password_must_reset = False
    await bump_security_version(session, user, reason="password_changed", request=request)
    await revoke_all_refresh_tokens(session, user.id)
    await session.commit()
    return {"ok": True}


@router.post("/api/auth/verify-email")
@router.post("/auth/verify-email")
async def verify_email_alias(
    body: EmailVerificationConfirm,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(timezone.utc)
    user_hint = None
    if body.code is not None:
        normalized_email = str(body.email).strip().lower()
        user_hint = (
            await session.execute(
                select(User).where(
                    func.lower(User.email) == normalized_email,
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if user_hint is None:
            raise HTTPException(status_code=400, detail="invalid_or_expired_verification_token")
        verification_hash = token_hash(f"{user_hint.id}:{body.code}")
    else:
        verification_hash = token_hash(body.token or "")
    result = await session.execute(
        select(VerificationToken)
        .where(
            VerificationToken.token_hash == verification_hash,
            VerificationToken.purpose == "email_verification",
        )
        .with_for_update()
    )
    token = result.scalar_one_or_none()
    if token is None or token.used_at is not None or token.invalidated_at is not None or token.expires_at <= now:
        raise HTTPException(status_code=400, detail="invalid_or_expired_verification_token")
    user = user_hint or await session.get(User, token.user_id, with_for_update=True)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_verification_token")
    token.used_at = now
    await session.execute(
        update(VerificationToken)
        .where(
            VerificationToken.user_id == user.id,
            VerificationToken.purpose == "email_verification",
            VerificationToken.id != token.id,
            VerificationToken.used_at.is_(None),
            VerificationToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    account_state = await account_security_for(session, user.id, for_update=True)
    account_state.email_verified_at = account_state.email_verified_at or now
    if account_state.account_status == "pending_email_verification":
        account_state.account_status = "active"
        user.is_active = True
        account_state.disabled_at = None
    await bump_security_version(session, user, reason="email_verified", request=request)
    await record_security_event(
        session,
        user_id=user.id,
        event_type="email_verification_completed",
        status="success",
        description="Email verification code consumed.",
        request=request,
    )
    await session.commit()
    return {"ok": True, "verified": True, "requires_verification": False}


@router.post("/api/auth/resend-verification")
@router.post("/auth/resend-verification")
async def resend_verification_alias(
    body: EmailVerificationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    normalized_email = str(body.email).strip().lower()
    ip = extract_client_ip(request)
    await check_action_rate_limit(
        session,
        email=normalized_email,
        ip=ip,
        detail="email_verification_request",
        maximum=get_settings().password_reset_rate_limit,
        window_minutes=30,
    )
    session.add(LoginAttempt(email=normalized_email, ip_address=ip, succeeded=True, detail="email_verification_request"))
    user = (
        await session.execute(select(User).where(func.lower(User.email) == normalized_email, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is not None:
        account_state = await account_security_for(session, user.id)
        if account_state.email_verified_at is None:
            result = await _queue_email_verification(session, user=user, request=request)
        else:
            result = {"requires_verification": False, "delivery_status": "not_required"}
    else:
        result = {"requires_verification": False, "delivery_status": "not_required"}
    await session.commit()
    return {"ok": True, **result}


@router.get("/api/auth/oauth/status")
async def oauth_status():
    provider_status = firebase_admin_configuration_status()
    configured = bool(provider_status.get("configured"))
    return {
        "data": {
            "google": configured,
            "apple": configured,
            "provider": "firebase",
            "configured": configured,
        }
    }


@router.post("/api/auth/phone/send-otp")
@router.post("/auth/phone/send-otp")
async def send_phone_otp(
    body: PhoneOtpSendRequest,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    ip = extract_client_ip(request)
    await check_action_rate_limit(
        session,
        email=user.email,
        ip=ip,
        detail="phone_otp_request",
        maximum=get_settings().otp_rate_limit,
        window_minutes=30,
    )
    session.add(LoginAttempt(email=user.email, ip_address=ip, succeeded=True, detail="phone_otp_request"))
    code = await create_phone_otp_token(
        session,
        user=user,
        phone=body.phone,
        purpose=body.purpose,
        request=request,
    )
    status, provider_meta = await _deliver_phone_otp_or_raise(body.phone, code)
    session.add(
        WHATSAPP_OUTBOX(
            user_id=user.id,
            title="Phone verification",
            phone=body.phone,
            message="Phone verification request processed. Raw OTP is not persisted.",
            status=status,
            extra_data={"purpose": body.purpose, **provider_meta},
        )
    )
    await record_security_event(
        session,
        user_id=user.id,
        event_type="phone_otp_requested",
        status=status,
        description="Phone OTP generated.",
        request=request,
    )
    await session.commit()
    payload: dict[str, Any] = {"ok": True, "delivery_status": status, "expires_in_minutes": 10}
    if get_settings().fixtures_enabled:
        payload["test_otp"] = code
    return payload


@router.post("/api/auth/phone/verify-otp")
@router.post("/auth/phone/verify-otp")
async def verify_phone_otp(
    body: PhoneOtpVerifyRequest,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(PhoneOtpToken)
        .where(
            PhoneOtpToken.user_id == user.id,
            PhoneOtpToken.phone == body.phone,
            PhoneOtpToken.purpose == body.purpose,
            PhoneOtpToken.used_at.is_(None),
            PhoneOtpToken.invalidated_at.is_(None),
        )
        .order_by(PhoneOtpToken.created_at.desc())
        .with_for_update()
    )
    otp = result.scalars().first()
    if otp is None or otp.expires_at <= now:
        raise HTTPException(status_code=400, detail="invalid_or_expired_otp")
    if otp.attempts >= 5:
        otp.invalidated_at = now
        raise HTTPException(status_code=429, detail="too_many_otp_attempts")
    if token_hash(body.otp) != otp.otp_hash:
        otp.attempts += 1
        await record_security_event(
            session,
            user_id=user.id,
            event_type="phone_otp_failed",
            status="blocked",
            description="Incorrect OTP submitted.",
            request=request,
        )
        raise HTTPException(status_code=400, detail="invalid_or_expired_otp")
    otp.used_at = now
    await session.execute(
        update(PhoneOtpToken)
        .where(
            PhoneOtpToken.user_id == user.id,
            PhoneOtpToken.phone == body.phone,
            PhoneOtpToken.purpose == body.purpose,
            PhoneOtpToken.id != otp.id,
            PhoneOtpToken.used_at.is_(None),
            PhoneOtpToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    account_state = await account_security_for(session, user.id, for_update=True)
    account_state.phone_verified_at = account_state.phone_verified_at or now
    profile = (await session.execute(select(Profile).where(Profile.user_id == user.id).with_for_update())).scalar_one_or_none()
    if profile is not None:
        profile.phone = body.phone
    await bump_security_version(session, user, reason="phone_verified", request=request)
    await record_security_event(
        session,
        user_id=user.id,
        event_type="phone_otp_verified",
        status="success",
        description="Phone OTP consumed.",
        request=request,
    )
    await session.commit()
    return {"ok": True, "verified": True}


@router.get("/api/profile/addresses")
async def list_profile_addresses(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["customer_addresses"]
    rows = (
        await session.execute(
            select(model)
            .where(model.user_id == user.id, model.deleted_at.is_(None))
            .order_by(model.is_default.desc(), model.created_at.desc())
        )
    ).scalars().all()
    return {"data": [serialize_record(row) for row in rows]}


@router.post("/api/profile/addresses", status_code=201)
async def create_profile_address(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    model = MODEL_BY_TABLE["customer_addresses"]
    row = model(
        user_id=user.id,
        label=str(body.get("label") or body.get("name") or "Address"),
        recipient_name=str(body.get("recipient_name") or body.get("recipientName") or body.get("fullName") or ""),
        phone=str(body.get("phone") or ""),
        governorate=str(body.get("governorate") or body.get("state") or ""),
        city=str(body.get("city") or ""),
        address=str(body.get("address") or body.get("line1") or ""),
        latitude=body.get("latitude"),
        longitude=body.get("longitude"),
        is_default=bool(body.get("is_default") or body.get("isDefault")),
    )
    if row.is_default:
        existing = (
            await session.execute(select(model).where(model.user_id == user.id, model.is_default.is_(True)))
        ).scalars().all()
        for item in existing:
            item.is_default = False
    session.add(row)
    await session.commit()
    return {"data": serialize_record(row)}


@router.patch("/api/profile/addresses/{address_id}")
async def update_profile_address(
    address_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    model = MODEL_BY_TABLE["customer_addresses"]
    row = await session.get(model, address_id, with_for_update=True)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="address_not_found")
    mapping = {
        "label": "label",
        "name": "label",
        "recipient_name": "recipient_name",
        "recipientName": "recipient_name",
        "fullName": "recipient_name",
        "phone": "phone",
        "governorate": "governorate",
        "state": "governorate",
        "city": "city",
        "address": "address",
        "line1": "address",
        "latitude": "latitude",
        "longitude": "longitude",
    }
    for source, target in mapping.items():
        if source in body:
            setattr(row, target, body[source])
    if "is_default" in body or "isDefault" in body:
        row.is_default = bool(body.get("is_default") or body.get("isDefault"))
        if row.is_default:
            others = (
                await session.execute(select(model).where(model.user_id == user.id, model.id != row.id, model.is_default.is_(True)))
            ).scalars().all()
            for item in others:
                item.is_default = False
    await session.commit()
    return {"data": serialize_record(row)}


@router.delete("/api/profile/addresses/{address_id}")
async def delete_profile_address(
    address_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    model = MODEL_BY_TABLE["customer_addresses"]
    row = await session.get(model, address_id, with_for_update=True)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="address_not_found")
    row.deleted_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


@router.get("/api/admin/staff/members")
async def admin_staff_members(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (
        await session.execute(
            select(User, Profile)
            .join(Profile, Profile.user_id == User.id, isouter=True)
            .join(UserRole, UserRole.user_id == User.id)
            .where(UserRole.role.in_(["admin", "manager", "finance", "logistics", "staff", "employee"]))
            .order_by(User.created_at.desc())
        )
    ).all()
    return {"data": [
        {
            "id": str(user.id),
            "user_id": str(user.id),
            "email": user.email,
            "full_name": profile.full_name if profile else "",
            "roles": [],
            "is_active": user.is_active,
        }
        for user, profile in rows
    ]}


@router.get("/api/admin/staff/roles")
async def admin_staff_roles(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(UserRole, Profile)
        .join(Profile, Profile.user_id == UserRole.user_id, isouter=True)
        .order_by(UserRole.created_at.desc())
    )).all()
    return {"data": [
        {
            "id": f"{row.user_id}:{row.role}",
            "user_id": str(row.user_id),
            "role": row.role,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "profile": {
                "full_name": profile.full_name if profile else None,
                "phone": profile.phone if profile else None,
            },
        }
        for row, profile in rows
    ]}


@router.get("/api/admin/users/options")
async def admin_user_options(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (
        await session.execute(
            select(User, Profile).join(Profile, Profile.user_id == User.id, isouter=True).order_by(User.email.asc())
        )
    ).all()
    return {"data": [
        {"id": str(user.id), "user_id": str(user.id), "email": user.email, "full_name": profile.full_name if profile else user.email}
        for user, profile in rows
    ]}


@router.post("/api/admin/staff/roles", status_code=201)
async def admin_add_staff_role(
    request: Request,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    user_id = uuid.UUID(str(body.get("user_id") or body.get("userId")))
    role = str(body.get("role") or "").strip()
    if not role:
        raise HTTPException(status_code=422, detail="role_required")
    existing = await session.get(UserRole, {"user_id": user_id, "role": role})
    if existing is None:
        existing = UserRole(user_id=user_id, role=role)
        session.add(existing)
    await session.commit()
    return {"data": {"id": f"{user_id}:{role}", "user_id": str(user_id), "role": role}}


@router.patch("/api/admin/staff/roles/{role_id}")
async def admin_update_staff_role(
    role_id: str,
    request: Request,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    old_user_id, old_role = _parse_role_id(role_id)
    body = await request.json()
    new_role = str(body.get("role") or old_role).strip()
    row = await session.get(UserRole, {"user_id": old_user_id, "role": old_role})
    if row is None:
        raise HTTPException(status_code=404, detail="role_not_found")
    if new_role != old_role:
        await session.delete(row)
        row = UserRole(user_id=old_user_id, role=new_role)
        session.add(row)
    await session.commit()
    return {"data": {"id": f"{old_user_id}:{new_role}", "user_id": str(old_user_id), "role": new_role}}


@router.delete("/api/admin/staff/roles/{role_id}")
async def admin_delete_staff_role(
    role_id: str,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user_id, role = _parse_role_id(role_id)
    row = await session.get(UserRole, {"user_id": user_id, "role": role})
    if row is None:
        raise HTTPException(status_code=404, detail="role_not_found")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.get("/api/admin/staff/permissions/catalog")
async def admin_staff_permissions_catalog(_: User = Depends(require_admin)):
    return {"data": PERMISSION_GROUPS}


@router.get("/api/admin/staff/permissions/{user_id}")
async def admin_staff_permissions(
    user_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    row = await session.get(StaffPermissionSet, user_id)
    return {
        "data": {
            "user_id": str(user_id),
            "permissions": normalize_permissions(row.permissions) if row is not None else sorted(
                await effective_permissions(session, user_id, set((await session.execute(
                    select(UserRole.role).where(UserRole.user_id == user_id)
                )).scalars()))
            ),
            "custom": row is not None,
            "available_permissions": sorted(ALL_PERMISSIONS),
        }
    }


@router.put("/api/admin/staff/permissions/{user_id}")
async def admin_save_staff_permissions(
    user_id: uuid.UUID,
    request: Request,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    target_roles = set((await session.execute(select(UserRole.role).where(UserRole.user_id == user_id))).scalars())
    if "admin" in target_roles:
        raise HTTPException(status_code=403, detail="protected_staff_account")
    body = await request.json()
    permissions = normalize_permissions(body.get("permissions"))
    row = await session.get(StaffPermissionSet, user_id)
    if row is None:
        row = StaffPermissionSet(user_id=user_id, permissions=permissions)
        session.add(row)
    else:
        row.permissions = permissions
    await session.commit()
    return {"data": {"user_id": str(user_id), "permissions": permissions, "custom": True}}


@router.post("/api/admin/profiles/lookup")
async def admin_profiles_lookup(
    request: Request,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    ids = [uuid.UUID(str(item)) for item in body.get("user_ids", []) if item]
    if not ids:
        return {"data": []}
    rows = (await session.execute(select(Profile).where(Profile.user_id.in_(ids)))).scalars().all()
    return {"data": [serialize_record(row) for row in rows]}


def _parse_role_id(role_id: str) -> tuple[uuid.UUID, str]:
    try:
        user_id, role = role_id.split(":", 1)
        return uuid.UUID(user_id), role
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_role_id")


@router.post("/me/password/session")
async def reject_password_without_current_password(user: User = Depends(current_user)):
    raise HTTPException(status_code=400, detail="current_password_required")


@router.post("/api/auth/forgot-password")
@router.post("/auth/password-reset-request")
@router.post("/auth/password-reset")
async def password_reset_request(
    body: PasswordResetRequest,
    request: Request,
    session: AsyncSession = Depends(_session_after_json),
):
    email = str(body.email).strip().lower()
    redirect_target = _resolve_password_reset_redirect(body.redirect_to, body.client_type)
    ip = extract_client_ip(request)
    await check_action_rate_limit(
        session,
        email=email or "invalid",
        ip=ip,
        detail="password_reset_request",
        maximum=get_settings().password_reset_rate_limit,
        window_minutes=30,
    )
    session.add(LoginAttempt(
        email=email or "invalid",
        ip_address=ip,
        succeeded=True,
        detail="password_reset_request",
    ))
    user = (await session.execute(
        select(User).where(func.lower(User.email) == email, User.deleted_at.is_(None))
    )).scalar_one_or_none()
    delivery_status = "not_required"
    if user is not None:
        settings = get_settings()
        if not settings.fixtures_enabled and not email_delivery_configured(settings):
            raise HTTPException(status_code=503, detail="password_reset_email_unconfigured")
        now = datetime.now(timezone.utc)
        old_tokens = (
            await session.execute(
                select(PasswordResetToken, PasswordResetTokenState)
                .join(
                    PasswordResetTokenState,
                    PasswordResetTokenState.reset_token_id == PasswordResetToken.id,
                    isouter=True,
                )
                .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
            )
        ).all()
        for old_reset, old_state in old_tokens:
            if old_state is None:
                session.add(PasswordResetTokenState(reset_token_id=old_reset.id, invalidated_at=now))
            elif old_state.invalidated_at is None:
                old_state.invalidated_at = now
        raw_token = secrets.token_urlsafe(48)
        reset_row = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash(raw_token),
            expires_at=now + timedelta(minutes=30),
            requested_ip=ip,
        )
        session.add(reset_row)
        await session.flush()
        reset_state = PasswordResetTokenState(reset_token_id=reset_row.id)
        session.add(reset_state)
        reset_link = _append_query_param(redirect_target, "token", raw_token)
        delivery_status = _email_delivery_status()
        email_row = EMAIL_OUTBOX(
            user_id=user.id,
            title="استعادة كلمة المرور",
            email=user.email,
            message=f"استخدم رابط استعادة كلمة المرور خلال 30 دقيقة:\n{reset_link}",
            status=delivery_status,
            extra_data={
                "reset_url": reset_link,
                "redirect_to": redirect_target,
                "client_type": body.client_type,
                "category": "security",
                "expires_in_minutes": 30,
            },
        )
        session.add(email_row)
        await session.flush()
        if not settings.fixtures_enabled:
            delivery = await deliver_email_now(session, email_row)
            delivery_status = delivery["status"]
            if delivery_status != "provider_accepted":
                reset_state.invalidated_at = datetime.now(timezone.utc)
                await session.commit()
                logger.warning(
                    "password_reset_email_delivery_failed provider=%s error_code=%s",
                    delivery.get("provider") or "none",
                    delivery.get("error_code") or "unknown",
                )
                raise HTTPException(status_code=503, detail="password_reset_email_delivery_failed")
        await record_security_event(
            session,
            user_id=user.id,
            event_type="password_reset_requested",
            status=delivery_status,
            description="Password reset token generated and previous active tokens invalidated.",
            request=request,
        )
    await session.commit()
    return {"ok": True, "delivery_status": delivery_status}


@router.post("/auth/password-reset-confirm")
async def password_reset_confirm(
    body: PasswordResetConfirm,
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash(body.token))
        .with_for_update()
    )
    reset = result.scalar_one_or_none()
    reset_state = await session.get(PasswordResetTokenState, reset.id) if reset is not None else None
    if reset is None or reset.used_at is not None or (reset_state is not None and reset_state.invalidated_at is not None) or reset.expires_at <= now:
        raise HTTPException(status_code=400, detail="invalid_or_expired_reset_token")
    user = await session.get(User, reset.user_id, with_for_update=True)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_reset_token")
    try:
        validate_password(body.new_password, email=user.email)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    same_password, _ = await asyncio.to_thread(verify_password, body.new_password, user.password_hash, user.password_salt)
    if same_password:
        raise HTTPException(status_code=409, detail="new_password_same_as_current")
    user.password_hash = await asyncio.to_thread(hash_password, body.new_password)
    user.password_salt = None
    user.password_must_reset = False
    reset.used_at = now
    if reset_state is None:
        session.add(PasswordResetTokenState(reset_token_id=reset.id))
    old_tokens = (
        await session.execute(
            select(PasswordResetToken, PasswordResetTokenState)
            .join(
                PasswordResetTokenState,
                PasswordResetTokenState.reset_token_id == PasswordResetToken.id,
                isouter=True,
            )
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.id != reset.id,
                PasswordResetToken.used_at.is_(None),
            )
        )
    ).all()
    for old_reset, old_state in old_tokens:
        if old_state is None:
            session.add(PasswordResetTokenState(reset_token_id=old_reset.id, invalidated_at=now))
        elif old_state.invalidated_at is None:
            old_state.invalidated_at = now
    await bump_security_version(session, user, reason="password_reset_completed")
    await revoke_all_refresh_tokens(session, user.id, now=now)
    await session.commit()
    return {"ok": True}


@router.post("/me/account-deletion-request")
async def request_account_deletion(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    existing = (
        await session.execute(
            select(AccountDeletionRequest)
            .where(
                AccountDeletionRequest.user_id == user.id,
                AccountDeletionRequest.status.in_(("pending", "processing")),
                AccountDeletionRequest.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="account_deletion_already_pending")
    now = datetime.now(timezone.utc)
    session.add(AccountDeletionRequest(
        user_id=user.id,
        status="pending",
        reason=str(body.get("reason") or ""),
    ))
    account_state = await account_security_for(session, user.id, for_update=True)
    account_state.account_status = "deletion_pending"
    user.is_active = False
    account_state.disabled_at = now
    await bump_security_version(session, user, reason="account_deletion_requested", request=request)
    await revoke_all_refresh_tokens(session, user.id, now=now)
    await session.commit()
    return {"ok": True, "status": "pending"}
