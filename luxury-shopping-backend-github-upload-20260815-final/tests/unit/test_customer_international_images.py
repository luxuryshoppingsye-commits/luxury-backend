from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException
from starlette.responses import FileResponse
from backend.app.api.routes import operations
from backend.app.models.domain import FileAsset


def records(tmp_path):
    owner_id, order_id, file_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    image = tmp_path / "dress.webp"
    image.write_bytes(b"RIFF-dress-image")
    order = SimpleNamespace(id=order_id, user_id=owner_id, deleted_at=None,
        extra_data={"items": [{"product_name": "فستان", "image_url": f"file:{file_id}"}]})
    asset = SimpleNamespace(id=file_id, owner_user_id=owner_id, deleted_at=None,
        policy_key="customer_request_attachment", status="available", scan_status="clean",
        content_type="image/webp", storage_provider="local_uploads", storage_key="private/dress.webp")
    async def get(model, identity):
        if model is FileAsset: return asset if identity == file_id else None
        return order if identity == order_id else None
    return order, asset, image, SimpleNamespace(get=AsyncMock(side_effect=get))


@pytest.mark.asyncio
async def test_customer_can_read_own_order_image_without_public_caching(tmp_path, monkeypatch):
    order, asset, image, session = records(tmp_path)
    monkeypatch.setattr(operations.storage, "_safe_join", lambda _: image)
    response = await operations.api_customer_international_order_attachment(order.id, asset.id,
        user=SimpleNamespace(id=order.user_id), session=session)
    assert isinstance(response, FileResponse)
    assert response.path == image
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_other_customer_cannot_read_an_order_image(tmp_path):
    order, asset, _, session = records(tmp_path)
    with pytest.raises(HTTPException) as error:
        await operations.api_customer_international_order_attachment(order.id, asset.id,
            user=SimpleNamespace(id=uuid.uuid4()), session=session)
    assert error.value.status_code == 404
    assert session.get.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unlinked", "foreign_asset", "infected", "deleted"])
async def test_customer_cannot_read_unlinked_or_unsafe_images(tmp_path, change):
    order, asset, _, session = records(tmp_path)
    if change == "unlinked": order.extra_data = {"items": []}
    if change == "foreign_asset": asset.owner_user_id = uuid.uuid4()
    if change == "infected": asset.scan_status = "infected"
    if change == "deleted": asset.deleted_at = "2026-10-12"
    with pytest.raises(HTTPException) as error:
        await operations.api_customer_international_order_attachment(order.id, asset.id,
            user=SimpleNamespace(id=order.user_id), session=session)
    assert error.value.status_code == 404
