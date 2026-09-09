from __future__ import annotations

from backend.app.services.payment_methods import (
    COD_PAYMENT_METHOD,
    _default_method_rows,
    normalize_payment_method_key,
    normalize_payment_method_rows,
    payment_account_options,
    payment_method_has_recipient,
    payment_method_recipients,
    payment_methods_payload,
)


def test_default_payment_methods_disable_cash_on_delivery() -> None:
    rows = _default_method_rows()

    cash = next(row for row in rows if row["provider_key"] == COD_PAYMENT_METHOD)
    assert cash["is_active"] is False
    assert next(row for row in rows if row["provider_key"] == "WALLET_TRANSFER")["is_active"] is False
    assert next(row for row in rows if row["provider_key"] == "BANK_TRANSFER")["is_active"] is False
    assert any(row["is_active"] for row in rows if row["provider_key"] != COD_PAYMENT_METHOD)
    assert payment_method_recipients(
        next(row for row in rows if row["provider_key"] == "YEMEN_WALLET")
    ) == [
        {"label": "الكريمي - إيداع يمني", "value": "3087726117"},
        {"label": "الكريمي - إيداع سعودي", "value": "3101858013"},
        {"label": "الكريمي - إيداع دولار", "value": "3101751294"},
        {"label": "رقم الهاتف", "value": "781010460"},
    ]


def test_payment_method_aliases_are_normalized() -> None:
    assert normalize_payment_method_key("cash") == COD_PAYMENT_METHOD
    assert normalize_payment_method_key("haseb_kuraimi") == "HASEB_KURAIMI"
    assert normalize_payment_method_key("bank_transfer") == "BANK_TRANSFER"


def test_existing_configuration_without_recipients_inherits_luxury_accounts() -> None:
    rows = normalize_payment_method_rows(
        [{"provider_key": "JAIB", "is_active": True}],
        base_rows=_default_method_rows(),
    )

    jaib = next(row for row in rows if row["provider_key"] == "JAIB")
    assert payment_method_recipients(jaib) == [
        {"label": "رقم حساب جيب", "value": "549179"},
    ]


def test_admin_update_preserves_unmentioned_methods_and_changes_toggle() -> None:
    current = _default_method_rows()
    rows = normalize_payment_method_rows(
        [{"provider_key": COD_PAYMENT_METHOD, "is_active": True}],
        base_rows=current,
    )

    cash = next(row for row in rows if row["provider_key"] == COD_PAYMENT_METHOD)
    haseb = next(row for row in rows if row["provider_key"] == "HASEB_KURAIMI")
    assert cash["is_active"] is True
    assert haseb["is_active"] is True
    assert payment_methods_payload(rows)["cod_enabled"] is True


def test_payment_account_options_excludes_unconfigured_transfer_methods() -> None:
    rows = normalize_payment_method_rows(
        [
            {
                "provider_key": "JAIB",
                "is_active": True,
                "merchant_number": "700123456",
                "transfer_recipients": [],
            },
            {
                "provider_key": "JAWALI",
                "is_active": True,
                "transfer_recipients": [],
            },
        ],
        base_rows=[
            {
                "provider_key": "JAIB",
                "name_ar": "جيب",
                "is_active": False,
                "sort_order": 20,
            },
            {
                "provider_key": "JAWALI",
                "name_ar": "جوالي",
                "is_active": False,
                "sort_order": 30,
            },
        ],
    )

    jaib = next(row for row in rows if row["provider_key"] == "JAIB")
    jawali = next(row for row in rows if row["provider_key"] == "JAWALI")
    assert payment_method_has_recipient(jaib) is True
    assert payment_method_has_recipient(jawali) is False
    assert payment_account_options(rows) == [
        {
            "id": "jaib",
            "payment_method": "JAIB",
            "display_name": "جيب",
            "account_name": "جيب",
            "account_number": "700123456",
            "merchant_number": "700123456",
            "phone_number": None,
            "type": "wallet",
            "is_active": True,
        }
    ]
