import pytest

from backend.app.api.routes.commerce import _order_status_notification_label


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("accepted", "تم اعتماد الطلب"),
        ("processing", "قيد التجهيز"),
        ("shipped", "تم شحن الطلب"),
        ("delivered", "تم تسليم الطلب"),
        ("cancelled", "تم إلغاء الطلب"),
    ],
)
def test_order_status_notification_label_is_arabic(status, expected):
    assert _order_status_notification_label(status) == expected


def test_unknown_status_keeps_the_original_value_for_diagnostics():
    assert _order_status_notification_label("custom_status") == "custom_status"
