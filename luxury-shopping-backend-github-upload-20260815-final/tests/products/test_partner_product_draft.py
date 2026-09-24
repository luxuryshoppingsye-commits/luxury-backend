import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routes.commerce import _partner_draft_payload, partner_product_draft
from app.models.domain import PartnerProductDraft


def test_draft_accepts_incomplete_real_product_data():
    payload = {
        "formData": {"name": "فستان صنعاني", "price": "", "tags": []},
        "productImages": [],
        "arImageUrl": "",
    }
    assert _partner_draft_payload(payload) == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"formData": {"partner_id": "someone-else"}, "productImages": [], "arImageUrl": ""},
        {"formData": {}, "productImages": ["x"] * 11, "arImageUrl": ""},
        {"formData": {}, "productImages": [], "arImageUrl": "x" * 2049},
    ],
)
def test_draft_rejects_unexpected_or_oversized_fields(payload):
    with pytest.raises(HTTPException) as error:
        _partner_draft_payload(payload)
    assert error.value.status_code == 422


def test_draft_read_is_scoped_to_signed_in_partner():
    user = SimpleNamespace(id=uuid4())

    class Session:
        async def get(self, model, key):
            assert model is PartnerProductDraft
            assert key == user.id
            return None

    assert asyncio.run(partner_product_draft(user=user, roles={"partner"}, session=Session())) == {"data": None}
    with pytest.raises(HTTPException) as error:
        asyncio.run(partner_product_draft(user=user, roles={"customer"}, session=Session()))
    assert error.value.status_code == 403
