from __future__ import annotations

from backend.app.services.payment_methods import (
    COD_PAYMENT_METHOD,
    _default_method_rows,
    normalize_payment_method_key,
    normalize_payment_method_rows,
    payment_methods_payload,
)


def test_default_payment_methods_disable_cash_on_delivery() -> None:
    rows = _default_method_rows()

    cash = next(row for row in rows if row["provider_key"] == COD_PAYMENT_METHOD)
    assert cash["is_active"] is False
    assert next(row for row in rows if row["provider_key"] == "WALLET_TRANSFER")["is_active"] is False
    assert next(row for row in rows if row["provider_key"] == "BANK_TRANSFER")["is_active"] is False
    assert any(row["is_active"] for row in rows if row["provider_key"] != COD_PAYMENT_METHOD)


def test_payment_method_aliases_are_normalized() -> None:
    assert normalize_payment_method_key("cash") == COD_PAYMENT_METHOD
    assert normalize_payment_method_key("haseb_kuraimi") == "HASEB_KURAIMI"
    assert normalize_payment_method_key("bank_transfer") == "BANK_TRANSFER"


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
