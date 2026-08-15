from __future__ import annotations

import asyncio
import random
import re
import socket
import smtplib
import ssl
from html import escape
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import parseaddr
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE


CLAIMABLE_STATUSES = {"pending", "queued", "failed_retryable"}
TERMINAL_STATUSES = {
    "provider_accepted",
    "sent",
    "delivered",
    "failed_permanent",
    "dead_letter",
    "blocked_configuration",
    "suppressed_by_preference",
    "suppressed_by_consent",
    "cancelled",
    "expired",
}
TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
PERMANENT_HTTP_STATUSES = {400, 401, 403, 404, 409, 410, 422}
EMAIL_RE = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$")
PHONE_RE = re.compile(r"^\+?[0-9]{8,18}$")
BRAND_LOGO_URL = "https://luxuryshoppings.com/assets/logo-OdLYDlxV.png"


class _IPv4SMTP(smtplib.SMTP):
    """SMTP client that avoids unreachable IPv6 routes on some hosts."""

    def _get_socket(self, host: str, port: int, timeout: float | None):
        last_error: OSError | None = None
        addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        for _family, _socktype, _proto, _canonname, sockaddr in addresses:
            try:
                return socket.create_connection(sockaddr, timeout)
            except OSError as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise OSError(f"No IPv4 address available for {host}")


class _IPv4SMTP_SSL(smtplib.SMTP_SSL):
    """SMTP-over-SSL client with IPv4 selection and hostname-based TLS."""

    def _get_socket(self, host: str, port: int, timeout: float | None):
        last_error: OSError | None = None
        addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        for _family, _socktype, _proto, _canonname, sockaddr in addresses:
            raw_socket = None
            try:
                raw_socket = socket.create_connection(sockaddr, timeout)
                return self.context.wrap_socket(raw_socket, server_hostname=host)
            except OSError as error:
                last_error = error
                if raw_socket is not None:
                    raw_socket.close()
        if last_error is not None:
            raise last_error
        raise OSError(f"No IPv4 address available for {host}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _extra(row: Any) -> dict[str, Any]:
    return dict(getattr(row, "extra_data", None) or {})


def _set_extra(row: Any, values: dict[str, Any]) -> None:
    row.extra_data = {**_extra(row), **values}


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _retry_delay_seconds(attempts: int, *, retry_after: int | None = None) -> int:
    settings = get_settings()
    if retry_after is not None and retry_after > 0:
        return min(retry_after, settings.message_retry_max_seconds)
    exponent = max(attempts - 1, 0)
    base = min(settings.message_retry_base_seconds * (2 ** exponent), settings.message_retry_max_seconds)
    jitter = random.randint(0, max(1, settings.message_retry_base_seconds))
    return min(base + jitter, settings.message_retry_max_seconds)


def _safe_error(error: Exception | str) -> str:
    text = str(error)
    for marker in ("Authorization", "Bearer", "password", "secret", "token"):
        text = text.replace(marker, "[redacted]")
    return text[:300]


def _recipient_email(row: Any) -> str:
    value = str(getattr(row, "email", None) or _extra(row).get("email") or _extra(row).get("to") or "").strip()
    _, parsed = parseaddr(value)
    return parsed.strip()


def _recipient_phone(row: Any) -> str:
    return str(getattr(row, "phone", None) or _extra(row).get("phone") or _extra(row).get("to") or "").strip()


def _email_valid(value: str) -> bool:
    return bool(value and EMAIL_RE.match(value)) and "\r" not in value and "\n" not in value


def _phone_valid(value: str) -> bool:
    compact = re.sub(r"[\s\-()]", "", value)
    return bool(PHONE_RE.match(compact))


def _email_provider_mode(settings: Any) -> str:
    requested = str(getattr(settings, "email_provider", "auto") or "auto").strip().lower()
    if requested in {"resend", "http"}:
        return "resend"
    if requested == "smtp":
        return "smtp"
    return "resend" if getattr(settings, "resend_api_key", "") and getattr(settings, "resend_from_email", "") else "smtp"


def email_delivery_configured(settings: Any | None = None) -> bool:
    settings = settings or get_settings()
    if _email_provider_mode(settings) == "resend":
        return bool(
            getattr(settings, "resend_api_key", "")
            and getattr(settings, "resend_api_url", "")
            and getattr(settings, "resend_from_email", "")
        )
    return bool(
        getattr(settings, "smtp_host", "")
        and getattr(settings, "smtp_username", "")
        and getattr(settings, "smtp_password", "")
        and (getattr(settings, "smtp_from_email", "") or getattr(settings, "smtp_username", ""))
    )


def _send_resend_email_sync(
    *,
    settings: Any,
    recipient: str,
    subject: str,
    plain_message: str,
    html_message: str,
) -> None:
    response = httpx.post(
        settings.resend_api_url,
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={
            "from": settings.resend_from_email,
            "to": [recipient],
            "subject": subject,
            "text": plain_message,
            "html": html_message,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ValueError("email_provider_missing_message_id")


async def _allowed_by_preferences(session: AsyncSession, row: Any, channel: str) -> tuple[bool, str | None]:
    user_id = getattr(row, "user_id", None)
    if user_id is None:
        return False, "recipient_user_required"
    pref_model = MODEL_BY_TABLE["notification_preferences"]
    pref = (
        await session.execute(
            select(pref_model).where(pref_model.user_id == user_id, pref_model.deleted_at.is_(None)).limit(1)
        )
    ).scalar_one_or_none()
    extra = _extra(row)
    category = str(extra.get("category") or extra.get("notification_category") or "system").lower()
    consent_required = bool(extra.get("consent_required", category in {"marketing", "promotional", "promotions"}))
    if consent_required and extra.get("consent") is False:
        return False, "communication_consent_required"
    if pref is None:
        return True, None
    if category in {"marketing", "promotional", "promotions"} and not bool(getattr(pref, "promotional_notifications", True)):
        return False, "communication_suppressed"
    if category == "order" and not bool(getattr(pref, "order_updates", True)):
        return False, "communication_suppressed"
    if category == "payment" and not bool(getattr(pref, "payment_updates", True)):
        return False, "communication_suppressed"
    if category == "shipping" and not bool(getattr(pref, "shipping_updates", True)):
        return False, "communication_suppressed"
    if category == "support" and not bool(getattr(pref, "support_updates", True)):
        return False, "communication_suppressed"
    if category == "system" and not bool(getattr(pref, "system_notifications", True)):
        return False, "communication_suppressed"
    return True, None


def _send_email_sync(
    recipient: str,
    subject: str,
    message: str,
    extra_data: dict[str, Any] | None = None,
) -> None:
    settings = get_settings()
    extra = extra_data or {}
    email = EmailMessage(policy=SMTP)
    email["From"] = settings.smtp_from_email or settings.smtp_username
    email["To"] = recipient
    email["Subject"] = str(subject or "Luxury Shopping")[:200].replace("\r", " ").replace("\n", " ")
    email.set_content(str(message or ""))
    logo_url = BRAND_LOGO_URL
    safe_subject = escape(str(subject or "رفاهية التسوق"))
    safe_message = escape(str(message or "")).replace("\n", "<br>")
    action_url = str(extra.get("verification_url") or extra.get("reset_url") or "").strip()
    action_label = str(extra.get("action_label") or "فتح الرابط").strip()
    action_html = ""
    if action_url and action_url.startswith(("https://", "http://")):
        action_html = (
            f'<p style="margin:28px 0 8px;text-align:center">'
            f'<a href="{escape(action_url, quote=True)}" '
            'style="display:inline-block;background:#976817;color:#fff;text-decoration:none;'
            'padding:14px 28px;border-radius:10px;font-weight:700">'
            f"{escape(action_label)}</a></p>"
            f'<p style="font-size:12px;color:#6b7280;word-break:break-all;text-align:center">'
            f"إذا لم يعمل الزر، انسخ هذا الرابط:<br>{escape(action_url)}</p>"
        )
    plain_message = str(message or "رفاهية التسوق")
    if action_url and action_url.startswith(("https://", "http://")):
        plain_message = f"{plain_message}\n\n{action_label}: {action_url}"
    email.set_content(plain_message)
    email.add_alternative(
        f"""<!doctype html>
<html lang=\"ar\" dir=\"rtl\"><body style=\"margin:0;background:#f7f3ec;font-family:Arial,sans-serif;color:#172033\">
<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"padding:32px 12px\"><tr><td align=\"center\">
<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"max-width:620px;background:#fff;border-radius:20px;overflow:hidden;box-shadow:0 10px 30px rgba(23,32,51,.08)\">
<tr><td style=\"background:#172033;padding:26px;text-align:center\"><img src=\"{escape(logo_url, quote=True)}\" alt=\"رفاهية التسوق\" width=\"180\" style=\"display:block;margin:0 auto;max-width:180px;height:auto\"></td></tr>
 <tr><td style=\"padding:34px 30px\"><p style=\"margin:0 0 12px;color:#9a6a05;font-size:14px;font-weight:700\">رفاهية التسوق</p><h1 style=\"margin:0 0 22px;font-size:25px\">{safe_subject}</h1><p style=\"font-size:16px;line-height:1.9;margin:0\">{safe_message}</p>{action_html}</td></tr>
<tr><td style=\"padding:18px 30px;background:#faf8f4;color:#6b7280;text-align:center;font-size:12px\">هذه رسالة آلية من رفاهية التسوق. لا ترد على هذا البريد.</td></tr>
</table></td></tr></table></body></html>""",
        subtype="html",
    )
    if _email_provider_mode(settings) == "resend":
        html_part = email.get_body(preferencelist=("html",))
        _send_resend_email_sync(
            settings=settings,
            recipient=recipient,
            subject=str(subject or "Luxury Shopping")[:200],
            plain_message=plain_message,
            html_message=html_part.get_content() if html_part is not None else "",
        )
        return
    if settings.smtp_port == 465:
        with _IPv4SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ssl.create_default_context()) as client:
            client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(email)
        return
    with _IPv4SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
        client.starttls()
        client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(email)


async def _claim_rows(session: AsyncSession, table: str, limit: int) -> list[Any]:
    model = MODEL_BY_TABLE[table]
    rows = list(
        (
            await session.execute(
                select(model)
                .where(model.status.in_(CLAIMABLE_STATUSES), model.deleted_at.is_(None))
                .order_by(model.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).scalars()
    )
    now = _now()
    claimed: list[Any] = []
    for row in rows:
        extra = _extra(row)
        if _parse_dt(extra.get("expires_at")) and _parse_dt(extra.get("expires_at")) <= now:
            row.status = "expired"
            _set_extra(row, {"last_error_code": "message_expired", "processed_at": _iso(now)})
            continue
        next_attempt = _parse_dt(extra.get("next_attempt_at") or extra.get("available_at"))
        if next_attempt and next_attempt > now:
            continue
        row.status = "processing"
        _set_extra(
            row,
            {
                "locked_at": _iso(now),
                "lock_expires_at": _iso(now + timedelta(seconds=get_settings().message_lock_timeout_seconds)),
            },
        )
        claimed.append(row)
    await session.flush()
    return claimed


def _mark_retry(row: Any, attempts: int, error_code: str, error: Exception | str, *, retry_after: int | None = None) -> None:
    settings = get_settings()
    now = _now()
    if attempts >= settings.message_max_attempts:
        row.status = "dead_letter"
        next_attempt_at = None
    else:
        row.status = "failed_retryable"
        next_attempt_at = now + timedelta(seconds=_retry_delay_seconds(attempts, retry_after=retry_after))
    _set_extra(
        row,
        {
            "attempts": attempts,
            "max_attempts": settings.message_max_attempts,
            "last_error_code": error_code,
            "last_error_safe": _safe_error(error),
            "last_failure_at": _iso(now),
            "next_attempt_at": _iso(next_attempt_at) if next_attempt_at else None,
            "locked_at": None,
            "lock_expires_at": None,
        },
    )
    if hasattr(row, "failure_count"):
        row.failure_count = attempts


def _mark_terminal(row: Any, status: str, *, code: str | None = None, provider_id: str | None = None) -> None:
    now = _now()
    row.status = status
    _set_extra(
        row,
        {
            "provider_message_id": provider_id,
            "last_error_code": code,
            "processed_at": _iso(now),
            "locked_at": None,
            "lock_expires_at": None,
        },
    )


async def process_email_outbox(session: AsyncSession, limit: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    limit = limit or settings.message_batch_size
    configured = email_delivery_configured(settings)
    rows = await _claim_rows(session, "email_outbox", limit)
    counts = {"configured": configured, "claimed": len(rows), "provider_accepted": 0, "retry_scheduled": 0, "failed_permanent": 0, "dead_letter": 0, "blocked_configuration": 0, "suppressed": 0}
    for row in rows:
        extra = _extra(row)
        attempts = int(extra.get("attempts") or 0) + 1
        _set_extra(row, {"attempts": attempts})
        allowed, suppression = await _allowed_by_preferences(session, row, "email")
        if not allowed:
            _mark_terminal(row, "suppressed_by_consent" if suppression == "communication_consent_required" else "suppressed_by_preference", code=suppression)
            counts["suppressed"] += 1
            continue
        recipient = _recipient_email(row)
        if not _email_valid(recipient):
            _mark_terminal(row, "failed_permanent", code="invalid_email")
            counts["failed_permanent"] += 1
            continue
        if not configured:
            _mark_terminal(row, "blocked_configuration", code="delivery_configuration_missing")
            counts["blocked_configuration"] += 1
            continue
        try:
            await asyncio.to_thread(
                _send_email_sync,
                recipient,
                row.title or "Luxury Shopping",
                row.message or "",
                extra,
            )
            _mark_terminal(row, "provider_accepted", provider_id=f"{_email_provider_mode(settings)}:accepted")
            counts["provider_accepted"] += 1
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
            code = getattr(error, "smtp_code", None)
            if isinstance(code, int) and 400 <= code < 500:
                _mark_retry(row, attempts, f"smtp_{code}", error)
                counts["dead_letter" if row.status == "dead_letter" else "retry_scheduled"] += 1
            else:
                _mark_terminal(row, "failed_permanent", code=f"smtp_{code or 'permanent'}")
                counts["failed_permanent"] += 1
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, smtplib.SMTPHeloError, OSError, TimeoutError) as error:
            _mark_retry(row, attempts, error.__class__.__name__, error)
            counts["dead_letter" if row.status == "dead_letter" else "retry_scheduled"] += 1
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            if status_code in PERMANENT_HTTP_STATUSES:
                _mark_terminal(row, "failed_permanent", code=f"email_provider_http_{status_code}")
                counts["failed_permanent"] += 1
            else:
                _mark_retry(row, attempts, f"email_provider_http_{status_code}", error)
                counts["dead_letter" if row.status == "dead_letter" else "retry_scheduled"] += 1
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            _mark_retry(row, attempts, error.__class__.__name__, error)
            counts["dead_letter" if row.status == "dead_letter" else "retry_scheduled"] += 1
    await session.flush()
    return counts


async def process_whatsapp_outbox(session: AsyncSession, limit: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    limit = limit or settings.message_batch_size
    configured = bool(settings.whatsapp_provider_url and settings.whatsapp_access_token)
    rows = await _claim_rows(session, "whatsapp_outbox", limit)
    counts = {"configured": configured, "claimed": len(rows), "provider_accepted": 0, "retry_scheduled": 0, "failed_permanent": 0, "dead_letter": 0, "blocked_configuration": 0, "suppressed": 0}
    for row in rows:
        extra = _extra(row)
        attempts = int(extra.get("attempts") or 0) + 1
        _set_extra(row, {"attempts": attempts})
        allowed, suppression = await _allowed_by_preferences(session, row, "whatsapp")
        if not allowed:
            _mark_terminal(row, "suppressed_by_consent" if suppression == "communication_consent_required" else "suppressed_by_preference", code=suppression)
            counts["suppressed"] += 1
            continue
        phone = _recipient_phone(row)
        if not _phone_valid(phone):
            _mark_terminal(row, "failed_permanent", code="invalid_phone")
            counts["failed_permanent"] += 1
            continue
        if not configured:
            _mark_terminal(row, "blocked_configuration", code="delivery_configuration_missing")
            counts["blocked_configuration"] += 1
            continue
        headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    settings.whatsapp_provider_url,
                    headers=headers,
                    json={"to": phone, "message": row.message or "", "title": row.title or ""},
                )
            if response.status_code in TRANSIENT_HTTP_STATUSES:
                retry_after = response.headers.get("Retry-After")
                _mark_retry(row, attempts, f"http_{response.status_code}", f"provider_http_{response.status_code}", retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None)
                counts["dead_letter" if row.status == "dead_letter" else "retry_scheduled"] += 1
            elif response.status_code in PERMANENT_HTTP_STATUSES:
                _mark_terminal(row, "failed_permanent", code=f"http_{response.status_code}")
                counts["failed_permanent"] += 1
            else:
                response.raise_for_status()
                _mark_terminal(row, "provider_accepted", provider_id=f"http:{response.status_code}")
                counts["provider_accepted"] += 1
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            if status_code in TRANSIENT_HTTP_STATUSES:
                retry_after = error.response.headers.get("Retry-After")
                _mark_retry(row, attempts, f"http_{status_code}", error, retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None)
                counts["dead_letter" if row.status == "dead_letter" else "retry_scheduled"] += 1
            else:
                _mark_terminal(row, "failed_permanent", code=f"http_{status_code}")
                counts["failed_permanent"] += 1
        except (httpx.TimeoutException, httpx.TransportError, OSError) as error:
            _mark_retry(row, attempts, error.__class__.__name__, error)
            counts["dead_letter" if row.status == "dead_letter" else "retry_scheduled"] += 1
    await session.flush()
    return counts
