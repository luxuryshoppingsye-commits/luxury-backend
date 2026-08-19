from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE


PAYMENT_METHODS_SETTING_KEY = "payment_methods"
COD_PAYMENT_METHOD = "CASH_ON_DELIVERY"

_METHOD_ALIASES = {
    "cash": COD_PAYMENT_METHOD,
    "cash_on_delivery": COD_PAYMENT_METHOD,
    "cash-on-delivery": COD_PAYMENT_METHOD,
    "haseb_kuraimi": "HASEB_KURAIMI",
    "jaib": "JAIB",
    "jawali": "JAWALI",
    "yemen_wallet": "YEMEN_WALLET",
    "one_cash": "ONE_CASH",
    "wallet_transfer": "WALLET_TRANSFER",
    "bank_transfer": "BANK_TRANSFER",
    "transfer": "BANK_TRANSFER",
}

_METHOD_DEFINITIONS = (
    {
        "provider_key": "HASEB_KURAIMI",
        "name_ar": "حاسب",
        "name_en": "Haseb",
        "mode": "automatic",
        "sort_order": 10,
        "requires_receipt": True,
        "requires_transaction_reference": True,
    },
    {
        "provider_key": "JAIB",
        "name_ar": "جيب",
        "name_en": "Jaib",
        "mode": "qr",
        "sort_order": 20,
        "requires_receipt": True,
        "requires_transaction_reference": True,
    },
    {
        "provider_key": "JAWALI",
        "name_ar": "جوالي",
        "name_en": "Jawali",
        "mode": "qr",
        "sort_order": 30,
        "requires_receipt": True,
        "requires_transaction_reference": True,
    },
    {
        "provider_key": "YEMEN_WALLET",
        "name_ar": "الكريمي",
        "name_en": "Alkuraimi",
        "mode": "manual",
        "sort_order": 40,
        "requires_receipt": False,
        "requires_transaction_reference": True,
    },
    {
        "provider_key": "ONE_CASH",
        "name_ar": "ون كاش",
        "name_en": "One Cash",
        "mode": "manual",
        "sort_order": 50,
        "requires_receipt": True,
        "requires_transaction_reference": True,
    },
    {
        "provider_key": "WALLET_TRANSFER",
        "name_ar": "تحويل محفظة",
        "name_en": "Wallet transfer",
        "mode": "manual",
        "sort_order": 60,
        "requires_receipt": True,
        "requires_transaction_reference": True,
    },
    {
        "provider_key": "BANK_TRANSFER",
        "name_ar": "تحويل بنكي",
        "name_en": "Bank transfer",
        "mode": "manual",
        "sort_order": 70,
        "requires_receipt": True,
        "requires_transaction_reference": True,
    },
    {
        "provider_key": COD_PAYMENT_METHOD,
        "name_ar": "الدفع عند الاستلام",
        "name_en": "Cash on Delivery",
        "mode": "cash_on_delivery",
        "sort_order": 100,
        "requires_receipt": False,
        "requires_transaction_reference": False,
    },
)


def normalize_payment_method_key(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    upper = normalized.upper()
    return _METHOD_ALIASES.get(normalized.lower(), upper)


def _allowed_method_keys() -> set[str]:
    return {
        normalize_payment_method_key(value)
        for value in get_settings().payment_method_allowlist
        if normalize_payment_method_key(value)
    }


def _default_method_rows() -> list[dict[str, Any]]:
    allowed = _allowed_method_keys()
    disabled_until_configured = {
        COD_PAYMENT_METHOD,
        "WALLET_TRANSFER",
        "BANK_TRANSFER",
    }
    rows = []
    for definition in _METHOD_DEFINITIONS:
        key = definition["provider_key"]
        row = {
            **definition,
            "is_active": key not in disabled_until_configured and key in allowed,
        }
        rows.append(row)
    return rows


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return default if value is None else bool(value)


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_payment_method_rows(
    raw_methods: Any,
    *,
    base_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_methods, list):
        raise HTTPException(status_code=422, detail="payment_methods_required")

    base = {
        row["provider_key"]: dict(row)
        for row in (base_rows or _default_method_rows())
    }
    supported = {definition["provider_key"] for definition in _METHOD_DEFINITIONS}
    for raw in raw_methods:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="invalid_payment_method_entry")
        key = normalize_payment_method_key(
            raw.get("provider_key") or raw.get("providerKey") or raw.get("key") or raw.get("payment_method")
        )
        if key not in supported:
            raise HTTPException(status_code=422, detail="unsupported_payment_method")
        row = base.get(key, {"provider_key": key})
        row["provider_key"] = key
        for field in (
            "name_ar",
            "name_en",
            "mode",
            "logo_url",
            "instructions_ar",
            "instructions_en",
            "account_number",
            "merchant_number",
            "phone_number",
            "qr_code_url",
        ):
            if field in raw:
                row[field] = _text(raw[field])
        for field in ("requires_receipt", "requires_transaction_reference", "is_active"):
            if field in raw:
                row[field] = _bool(raw[field])
        if "sort_order" in raw:
            try:
                row["sort_order"] = int(raw["sort_order"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="invalid_payment_method_sort_order")
        base[key] = row

    rows = list(base.values())
    rows.sort(key=lambda row: int(row.get("sort_order") or 0))
    return rows


async def read_payment_method_rows(session: AsyncSession) -> list[dict[str, Any]]:
    model = MODEL_BY_TABLE["site_settings"]
    result = await session.execute(
        select(model)
        .where(model.name == PAYMENT_METHODS_SETTING_KEY, model.deleted_at.is_(None))
        .limit(1)
    )
    row = result.scalar_one_or_none()
    extra = getattr(row, "extra_data", None) if row is not None else None
    if isinstance(extra, dict) and isinstance(extra.get("methods"), list):
        rows = normalize_payment_method_rows(extra["methods"])
        if extra.get("configured_by_admin") is not True:
            for method in rows:
                if method.get("provider_key") == COD_PAYMENT_METHOD:
                    method["is_active"] = False
        return rows
    return _default_method_rows()


def payment_methods_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "methods": rows,
        "cod_enabled": any(
            row.get("provider_key") == COD_PAYMENT_METHOD and row.get("is_active") is True
            for row in rows
        ),
    }


async def validate_payment_method_for_checkout(
    session: AsyncSession,
    value: Any,
) -> str:
    method = normalize_payment_method_key(value)
    if not method:
        raise HTTPException(status_code=422, detail="invalid_payment_method")
    allowed = _allowed_method_keys()
    if method not in allowed:
        raise HTTPException(status_code=422, detail="invalid_payment_method")
    rows = await read_payment_method_rows(session)
    if not any(row.get("provider_key") == method and row.get("is_active") is True for row in rows):
        raise HTTPException(status_code=409, detail="payment_method_disabled")
    return method
