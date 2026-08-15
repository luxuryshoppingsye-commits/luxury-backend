from types import SimpleNamespace

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
