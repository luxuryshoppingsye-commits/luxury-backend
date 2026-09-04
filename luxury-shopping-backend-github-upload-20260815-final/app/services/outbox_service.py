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
    headers: dict[str, str] | None = None,
) -> None:
    payload = {
        "from": settings.resend_from_email,
        "to": [recipient],
        "subject": subject,
        "text": plain_message,
        "html": html_message,
    }
    if headers:
        payload["headers"] = headers
    response = httpx.post(
        settings.resend_api_url,
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json=payload,
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


def _email_message_html(message: str) -> str:
    """Escape message content and turn ordinary HTTPS links into safe anchors."""
    escaped_message = escape(str(message or ""))
    linked_message = re.sub(
        r"(https?://[^\s<]+)",
        lambda match: (
            f'<a class="email-link" href="{match.group(1)}" style="color:#58a6ff;text-decoration:underline;'
            f'word-break:break-all">{match.group(1)}</a>'
        ),
        escaped_message,
    )
    return linked_message.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _render_branded_email_html(
    *,
    logo_url: str,
    safe_subject: str,
    safe_message: str,
    verification_code_html: str,
    action_html: str,
) -> str:
    """Render a light-first email that follows the recipient's color scheme."""
    return f"""<!doctype html>
<html lang=\"ar\" dir=\"rtl\">
<head>
<meta charset=\"utf-8\">
<meta name=\"x-apple-disable-message-reformatting\">
<meta name=\"format-detection\" content=\"telephone=no\">
<meta name=\"color-scheme\" content=\"light dark\">
<meta name=\"supported-color-schemes\" content=\"light dark\">
<style>
:root {{ color-scheme: light dark; supported-color-schemes: light dark; }}
html, body {{ margin: 0; padding: 0; width: 100%; }}
body {{ background-color: #f7f5ef; color: #202124; font-family: Arial, Tahoma, sans-serif; line-height: 1.6; }}
.email-page {{ background-color: #f7f5ef !important; }}
.email-container, .email-brand, .email-card {{ background-color: #fffdf8 !important; }}
.email-card {{ border-color: #e4e1d8 !important; }}
.email-heading, .email-text {{ color: #202124 !important; }}
.email-muted {{ color: #737780 !important; }}
.email-link {{ color: #1677c8 !important; }}
.email-divider {{ border-color: #e4e1d8 !important; }}
.email-code {{ color: #9a6900 !important; }}
@media (prefers-color-scheme: dark) {{
  html, body, .email-page {{ background-color: #111318 !important; color: #f0f2f5 !important; }}
  .email-container, .email-brand, .email-card {{ background-color: #1d2027 !important; }}
  .email-card {{ border-color: #3b414d !important; }}
  .email-heading, .email-text {{ color: #f0f2f5 !important; }}
  .email-muted {{ color: #aeb5c1 !important; }}
  .email-link {{ color: #f2bd39 !important; }}
  .email-divider {{ border-color: #3b414d !important; }}
  .email-code {{ color: #f2bd39 !important; }}
}}
[data-ogsc] .email-page {{ background-color: #111318 !important; }}
[data-ogsc] .email-container, [data-ogsc] .email-brand, [data-ogsc] .email-card {{ background-color: #1d2027 !important; }}
[data-ogsc] .email-card, [data-ogsc] .email-divider {{ border-color: #3b414d !important; }}
[data-ogsc] .email-heading, [data-ogsc] .email-text {{ color: #f0f2f5 !important; }}
[data-ogsc] .email-muted {{ color: #aeb5c1 !important; }}
[data-ogsc] .email-link, [data-ogsc] .email-code {{ color: #f2bd39 !important; }}
@media only screen and (max-width: 480px) {{
  .email-page {{ padding: 16px 8px !important; }}
  .email-card {{ padding: 16px 14px !important; }}
}}
</style>
</head>
<body dir=\"rtl\" class=\"email-page\" bgcolor=\"#f7f5ef\" style=\"margin:0;padding:0;background-color:#f7f5ef;color:#202124;font-family:Arial,Tahoma,sans-serif;line-height:1.6\">
<div style=\"display:none;max-height:0;overflow:hidden;opacity:0\">تجربة تسوق تليق بك من رفاهية التسوق</div>
<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" class=\"email-page\" bgcolor=\"#f7f5ef\" style=\"background-color:#f7f5ef;padding:24px 12px\"><tr><td align=\"center\">
<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" class=\"email-container\" bgcolor=\"#fffdf8\" style=\"max-width:420px;background-color:#fffdf8\">
<tr><td class=\"email-brand\" bgcolor=\"#fffdf8\" style=\"padding:8px 18px 18px;text-align:center;background-color:#fffdf8\"><img src=\"{escape(logo_url, quote=True)}\" alt=\"رفاهية التسوق\" width=\"24\" height=\"24\" style=\"display:block;margin:0 auto 8px;width:24px;height:24px;object-fit:contain\"><p class=\"email-text\" style=\"margin:0;color:#202124;font-size:11px;line-height:1.5;letter-spacing:normal\">رفاهية التسوق</p></td></tr>
<tr><td style=\"padding:0 0 16px;text-align:center;direction:rtl\"><h1 class=\"email-heading\" style=\"margin:0;color:#202124;font-size:21px;line-height:1.35;font-weight:700\">{safe_subject}</h1></td></tr>
<tr><td class=\"email-card\" bgcolor=\"#fffdf8\" style=\"border:1px solid #e4e1d8;border-radius:7px;padding:18px 16px;background-color:#fffdf8;text-align:right;direction:rtl\"><p class=\"email-text\" style=\"margin:0;color:#202124;font-size:15px;line-height:1.9\">{safe_message}</p>{verification_code_html}{action_html}</td></tr>
<tr><td class=\"email-muted\" style=\"padding:18px 10px 4px;color:#737780;text-align:center;font-size:11px;line-height:1.8\">هذه رسالة آلية من رفاهية التسوق.<br>إذا لم تطلب هذه الرسالة، يمكنك تجاهلها بأمان.</td></tr>
</table></td></tr></table></body></html>"""


def _send_email_sync(
    recipient: str,
    subject: str,
    message: str,
    extra_data: dict[str, Any] | None = None,
) -> None:
    settings = get_settings()
    extra = extra_data or {}
    sender = str(settings.smtp_from_email or settings.smtp_username or "").strip()
    # Google displays App Passwords in groups separated by spaces. Render may
    # preserve those spaces when the secret is pasted, but Gmail expects the
    # compact 16-character value during SMTP authentication.
    smtp_password = re.sub(r"\s+", "", str(settings.smtp_password or ""))
    email = EmailMessage(policy=SMTP)
    email["From"] = sender
    email["To"] = recipient
    email["Subject"] = str(subject or "Luxury Shopping")[:200].replace("\r", " ").replace("\n", " ")
    is_otp = str(extra.get("delivery_method") or "").strip().lower() == "otp"
    email.set_content(str(message or ""))
    if is_otp:
        # These are transactional security messages. The receiving mailbox still
        # decides final placement, but these headers preserve the user's intent
        # that the one-time code is high priority.
        email["Importance"] = "high"
        email["X-Priority"] = "1"
        email["X-MSMail-Priority"] = "High"
    logo_url = BRAND_LOGO_URL
    safe_subject = escape(str(subject or "رفاهية التسوق"))
    raw_message = str(message or "")
    action_url = "" if is_otp else str(extra.get("verification_url") or extra.get("reset_url") or "").strip()
    action_label = str(extra.get("action_label") or "فتح الرابط").strip()
    verification_code_match = re.search(r"(?:هو|is)\s*[:：]\s*([0-9]{4,10})", raw_message, flags=re.IGNORECASE)
    verification_code = verification_code_match.group(1) if verification_code_match else ""
    display_message = raw_message
    if verification_code:
        display_message = re.sub(
            rf"((?:هو|is)\s*[:：]\s*){re.escape(verification_code)}",
            r"\1",
            display_message,
            count=1,
            flags=re.IGNORECASE,
        )
    safe_message = _email_message_html(display_message)
    verification_code_html = (
        f'<div style="margin:20px 0 4px;text-align:center">'
        f'<div class="email-muted" style="color:#737780;font-size:11px;line-height:1.5">رمز التفعيل</div>'
        f'<div class="email-code" dir="ltr" style="margin-top:4px;color:#9a6900;font-size:24px;font-weight:700;letter-spacing:3px;line-height:1.3">{escape(verification_code)}</div></div>'
        if verification_code
        else ""
    )
    action_html = ""
    if action_url and action_url.startswith(("https://", "http://")):
        action_html = (
            f'<div class="email-divider" style="margin:20px 0 0;padding-top:14px;border-top:1px solid #e4e1d8">'
            f'<p class="email-muted" style="font-size:12px;color:#737780;line-height:1.7;margin:0 0 4px">{escape(action_label)}:</p>'
            f'<p dir="ltr" style="font-size:12px;word-break:break-all;line-height:1.8;margin:0">'
            f'<a class="email-link" href="{escape(action_url, quote=True)}" style="color:#58a6ff;text-decoration:underline;word-break:break-all">'
            f'{escape(action_url)}</a></p></div>'
    )
    plain_message = str(message or "رفاهية التسوق")
    if is_otp:
        plain_message = re.sub(r"https?://[^\s]+", "", plain_message).strip()
    if action_url and action_url.startswith(("https://", "http://")):
        plain_message = f"{plain_message}\n\n{action_label}: {action_url}"
    email.set_content(plain_message)
    email.add_alternative(
        _render_branded_email_html(
            logo_url=logo_url,
            safe_subject=safe_subject,
            safe_message=safe_message,
            verification_code_html=verification_code_html,
            action_html=action_html,
        ),
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
            headers={
                name: str(email[name])
                for name in ("Importance", "X-Priority", "X-MSMail-Priority")
                if email[name]
            },
        )
        return
    if settings.smtp_port == 465:
        with _IPv4SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ssl.create_default_context()) as client:
            client.login(settings.smtp_username.strip(), smtp_password)
            client.send_message(email)
        return
    with _IPv4SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
        client.starttls()
        client.login(settings.smtp_username.strip(), smtp_password)
        client.send_message(email)


async def deliver_email_now(session: AsyncSession, row: Any) -> dict[str, Any]:
    """Deliver a critical email before returning success to its caller.

    Password-reset messages must not be reported as sent merely because they
    were inserted into the outbox. The normal worker still handles retries for
    the rest of the system; this focused path proves provider acceptance for a
    security email while preserving the outbox audit row.
    """
    settings = get_settings()
    now = _now()
    row.status = "processing"
    extra = _extra(row)
    attempts = int(extra.get("attempts") or 0) + 1
    _set_extra(row, {"attempts": attempts, "started_at": _iso(now)})
    recipient = _recipient_email(row)
    if not _email_valid(recipient):
        _mark_terminal(row, "failed_permanent", code="invalid_email")
    elif not email_delivery_configured(settings):
        _mark_terminal(row, "blocked_configuration", code="delivery_configuration_missing")
    else:
        try:
            await asyncio.to_thread(
                _send_email_sync,
                recipient,
                row.title or "Luxury Shopping",
                row.message or "",
                extra,
            )
            _mark_terminal(row, "provider_accepted", provider_id=f"{_email_provider_mode(settings)}:accepted")
        except smtplib.SMTPAuthenticationError as error:
            code = getattr(error, "smtp_code", None)
            _mark_terminal(row, "failed_permanent", code=f"smtp_auth_{code or 'failed'}")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
            code = getattr(error, "smtp_code", None)
            if isinstance(code, int) and 400 <= code < 500:
                _mark_retry(row, attempts, f"smtp_{code}", error)
            else:
                _mark_terminal(row, "failed_permanent", code=f"smtp_{code or 'permanent'}")
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, smtplib.SMTPHeloError, OSError, TimeoutError) as error:
            _mark_retry(row, attempts, error.__class__.__name__, error)
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            if status_code in PERMANENT_HTTP_STATUSES:
                _mark_terminal(row, "failed_permanent", code=f"email_provider_http_{status_code}")
            else:
                _mark_retry(row, attempts, f"email_provider_http_{status_code}", error)
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            _mark_retry(row, attempts, error.__class__.__name__, error)
    await session.flush()
    return {
        "status": row.status,
        "provider": _email_provider_mode(settings) if row.status == "provider_accepted" else None,
        "error_code": _extra(row).get("last_error_code"),
    }


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
        except smtplib.SMTPAuthenticationError as error:
            code = getattr(error, "smtp_code", None)
            _mark_terminal(row, "failed_permanent", code=f"smtp_auth_{code or 'failed'}")
            counts["failed_permanent"] += 1
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
