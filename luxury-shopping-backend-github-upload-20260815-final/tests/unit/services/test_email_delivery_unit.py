from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import outbox_service


def test_email_provider_uses_resend_over_smtp_when_auto_configured() -> None:
    settings = SimpleNamespace(
        email_provider="auto",
        resend_api_key="resend_test_key",
        resend_api_url="https://api.resend.com/emails",
        resend_from_email="no-reply@luxuryshoppings.com",
        smtp_host="smtp.gmail.com",
        smtp_username="sender@example.com",
        smtp_password="app-password",
        smtp_from_email="sender@example.com",
    )

    assert outbox_service._email_provider_mode(settings) == "resend"
    assert outbox_service.email_delivery_configured(settings) is True


def test_email_provider_falls_back_to_smtp_when_resend_is_unconfigured() -> None:
    settings = SimpleNamespace(
        email_provider="auto",
        resend_api_key="",
        resend_api_url="https://api.resend.com/emails",
        resend_from_email="",
        smtp_host="smtp.gmail.com",
        smtp_username="sender@example.com",
        smtp_password="app-password",
        smtp_from_email="sender@example.com",
    )

    assert outbox_service._email_provider_mode(settings) == "smtp"
    assert outbox_service.email_delivery_configured(settings) is True


def test_email_provider_requires_verified_sender_for_resend() -> None:
    settings = SimpleNamespace(
        email_provider="resend",
        resend_api_key="resend_test_key",
        resend_api_url="https://api.resend.com/emails",
        resend_from_email="",
        smtp_host="smtp.gmail.com",
        smtp_username="sender@example.com",
        smtp_password="app-password",
        smtp_from_email="sender@example.com",
    )

    assert outbox_service._email_provider_mode(settings) == "resend"
    assert outbox_service.email_delivery_configured(settings) is False


def test_resend_payload_contains_arabic_content_logo_and_action_link(monkeypatch) -> None:
    settings = SimpleNamespace(
        email_provider="resend",
        resend_api_key="resend_test_key",
        resend_api_url="https://api.resend.com/emails",
        resend_from_email="no-reply@luxuryshoppings.com",
        smtp_host="",
        smtp_port=465,
        smtp_username="",
        smtp_password="",
        smtp_from_email="",
    )
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"id": "email_test_id"}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr(outbox_service, "get_settings", lambda: settings)
    monkeypatch.setattr(outbox_service.httpx, "post", fake_post)

    outbox_service._send_email_sync(
        "customer@example.com",
        "استعادة كلمة المرور",
        "اضغط الزر لاستعادة كلمة المرور.",
        {"reset_url": "https://luxuryshoppings.com/reset-password?token=test"},
    )

    payload = captured["json"]
    assert payload["from"] == "no-reply@luxuryshoppings.com"
    assert payload["to"] == ["customer@example.com"]
    assert "استعادة كلمة المرور" in payload["subject"]
    assert "رفاهية التسوق" in payload["html"]
    assert "logo-OdLYDlxV.png" in payload["html"]
    assert "????" not in payload["html"]


def test_smtp_strips_google_app_password_spaces_and_uses_branded_html(monkeypatch) -> None:
    settings = SimpleNamespace(
        email_provider="smtp",
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_username="luxuryshoppingsye@gmail.com",
        smtp_password="abcd efgh ijkl mnop",
        smtp_from_email="luxuryshoppingsye@gmail.com",
        resend_api_key="",
        resend_api_url="https://api.resend.com/emails",
        resend_from_email="",
    )
    captured = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def starttls(self):
            return None

        def login(self, username, password):
            captured["credentials"] = (username, password)

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr(outbox_service, "get_settings", lambda: settings)
    monkeypatch.setattr(outbox_service, "_IPv4SMTP", lambda *args, **kwargs: FakeClient())

    outbox_service._send_email_sync(
        "customer@example.com",
        "استعادة كلمة المرور",
        "اضغط الزر لاستعادة كلمة المرور.",
        {"reset_url": "https://luxuryshoppings.com/reset-password?token=test"},
    )

    assert captured["credentials"] == ("luxuryshoppingsye@gmail.com", "abcdefghijklmnop")
    html = captured["message"].get_body(preferencelist=("html",)).get_content()
    assert "https://luxuryshoppings.com/assets/logo-OdLYDlxV.png" in html
    assert "تجربة تسوق تليق بك" in html
    assert "letter-spacing:normal" in html


@pytest.mark.asyncio
async def test_critical_email_delivery_returns_provider_acceptance(monkeypatch) -> None:
    settings = SimpleNamespace(
        email_provider="resend",
        resend_api_key="resend_test_key",
        resend_api_url="https://api.resend.com/emails",
        resend_from_email="no-reply@luxuryshoppings.com",
        smtp_host="",
        smtp_username="",
        smtp_password="",
        smtp_from_email="",
    )
    sent = []

    def fake_send(recipient, subject, message, extra_data=None):
        sent.append((recipient, subject, message, extra_data))

    monkeypatch.setattr(outbox_service, "get_settings", lambda: settings)
    monkeypatch.setattr(outbox_service, "_send_email_sync", fake_send)
    row = SimpleNamespace(
        status="pending",
        email="customer@example.com",
        title="استعادة كلمة المرور",
        message="رابط الاختبار",
        extra_data={},
    )
    session = SimpleNamespace(flush=AsyncMock())

    result = await outbox_service.deliver_email_now(session, row)

    assert result["status"] == "provider_accepted"
    assert result["provider"] == "resend"
    assert sent and sent[0][0] == "customer@example.com"
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_critical_email_delivery_blocks_when_provider_is_unconfigured(monkeypatch) -> None:
    settings = SimpleNamespace(
        email_provider="resend",
        resend_api_key="",
        resend_api_url="https://api.resend.com/emails",
        resend_from_email="",
        smtp_host="",
        smtp_username="",
        smtp_password="",
        smtp_from_email="",
    )
    monkeypatch.setattr(outbox_service, "get_settings", lambda: settings)
    row = SimpleNamespace(
        status="pending",
        email="customer@example.com",
        title="استعادة كلمة المرور",
        message="رابط الاختبار",
        extra_data={},
    )
    session = SimpleNamespace(flush=AsyncMock())

    result = await outbox_service.deliver_email_now(session, row)

    assert result == {"status": "blocked_configuration", "provider": None, "error_code": "delivery_configuration_missing"}


@pytest.mark.asyncio
async def test_critical_email_delivery_reports_gmail_authentication_failure(monkeypatch) -> None:
    settings = SimpleNamespace(
        email_provider="smtp",
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_username="sender@example.com",
        smtp_password="app-password",
        smtp_from_email="sender@example.com",
        resend_api_key="",
        resend_api_url="https://api.resend.com/emails",
        resend_from_email="",
    )

    monkeypatch.setattr(outbox_service, "get_settings", lambda: settings)

    def fake_send(*_args, **_kwargs):
        raise outbox_service.smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")

    monkeypatch.setattr(outbox_service, "_send_email_sync", fake_send)
    row = SimpleNamespace(
        status="pending",
        email="customer@example.com",
        title="استعادة كلمة المرور",
        message="رابط الاختبار",
        extra_data={},
    )
    session = SimpleNamespace(flush=AsyncMock())

    result = await outbox_service.deliver_email_now(session, row)

    assert result == {"status": "failed_permanent", "provider": None, "error_code": "smtp_auth_535"}
