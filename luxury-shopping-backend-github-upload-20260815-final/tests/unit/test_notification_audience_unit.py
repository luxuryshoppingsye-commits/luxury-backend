from types import SimpleNamespace

from backend.app.api.routes.operations import _notification_recipient_is_customer


def test_notification_audience_accepts_explicit_customer_role() -> None:
    assert _notification_recipient_is_customer(None, {"customer"}) is True


def test_notification_audience_accepts_legacy_customer_profile_classification() -> None:
    profile = SimpleNamespace(classification="عميل", extra_data={})
    assert _notification_recipient_is_customer(profile, set()) is True


def test_notification_audience_does_not_override_an_explicit_non_customer_role() -> None:
    profile = SimpleNamespace(classification="عميل", extra_data={})
    assert _notification_recipient_is_customer(profile, {"partner"}) is False


def test_notification_audience_excludes_unclassified_accounts() -> None:
    profile = SimpleNamespace(classification=None, extra_data={})
    assert _notification_recipient_is_customer(profile, set()) is False
