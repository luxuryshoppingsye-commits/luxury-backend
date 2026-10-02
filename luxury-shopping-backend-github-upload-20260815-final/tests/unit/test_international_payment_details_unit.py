import pytest
from fastapi import HTTPException

from app.api.routes.operations import _validate_international_payment_details


def test_bank_transfer_requires_bank_and_reference() -> None:
    with pytest.raises(HTTPException) as missing_bank:
        _validate_international_payment_details(
            {"payment_method": "bank_transfer", "transfer_reference": "78451236"}
        )
    assert missing_bank.value.detail == "bank_name_required"

    with pytest.raises(HTTPException) as missing_reference:
        _validate_international_payment_details(
            {"payment_method": "bank_transfer", "payment_provider_name": "بنك الكريمي"}
        )
    assert missing_reference.value.detail == "transfer_reference_required"


def test_wallet_details_are_normalized_for_storage() -> None:
    payload = {
        "payment_method": "wallet_transfer",
        "wallet_name": "محفظة جيب",
        "reference_number": "WALLET-78451236",
    }

    _validate_international_payment_details(payload)

    assert payload["payment_method"] == "wallet"
    assert payload["payment_provider_name"] == "محفظة جيب"
    assert payload["wallet_name"] == "محفظة جيب"
    assert payload["transfer_reference"] == "WALLET-78451236"
