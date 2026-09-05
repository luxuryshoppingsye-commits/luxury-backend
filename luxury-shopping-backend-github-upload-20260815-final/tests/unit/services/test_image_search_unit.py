import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.app.services import image_search as service


def image_body():
    data = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(data, "PNG")
    return {"imageBase64": "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()}


@pytest.mark.parametrize("body", [{}, {"imageBase64": "invalid"}, {"imageBase64": "data:image/png;base64,YWJj"}])
def test_invalid_images_rejected(body):
    with pytest.raises(HTTPException) as exc:
        service._image_data(body)
    assert exc.value.status_code == 400


def test_image_reencoded_without_metadata():
    encoded = service._image_data(image_body())
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.format == "JPEG"
        assert image.size == (16, 16)


@pytest.mark.asyncio
async def test_missing_provider_is_not_replaced_by_random_products(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(gemini_api_key="", google_api_key="", ai_api_key=""))
    with pytest.raises(HTTPException) as exc:
        await service.search_catalog_image(image_body(), None)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_no_product_in_image_returns_no_matches_without_catalog_query(monkeypatch):
    monkeypatch.setattr(service, "_describe_image", AsyncMock(return_value={"typeTerms": [], "attributes": []}))
    result = await service.search_catalog_image(image_body(), None)
    assert result["products"] == []
    assert result["noMatches"] is True


@pytest.mark.asyncio
async def test_analysis_ranks_only_matching_catalog_products(monkeypatch):
    monkeypatch.setattr(service, "_describe_image", AsyncMock(return_value={
        "productType": "handbag", "typeTerms": ["handbag", "حقيبة"], "attributes": ["red", "حمراء"]}))
    def product(id, name):
        return SimpleNamespace(id=id, name=name, name_en="", description="", tags=[])
    rows = [product("blue", "blue handbag"), product("shoe", "red shoe"), product("red", "red handbag")]
    class Session:
        async def execute(self, statement):
            sql = str(statement)
            assert "approval_status" in sql
            assert "deleted_at IS NULL" in sql
            return SimpleNamespace(scalars=lambda: rows)
    monkeypatch.setattr(service, "serialize_record", lambda p: {"id": p.id, "name": p.name})
    result = await service.search_catalog_image(image_body(), Session())
    assert [p["id"] for p in result["products"]] == ["red", "blue"]
    assert result["searchInfo"]["source"] == "image_analysis"


@pytest.mark.asyncio
async def test_provider_receives_actual_image_bytes(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(
        gemini_api_key="test-key", google_api_key="", ai_api_key="", ai_default_model="gemini-2.5-flash", ai_request_timeout_seconds=10))
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json):
            inline = json["contents"][0]["parts"][1]["inlineData"]
            assert inline["mimeType"] == "image/jpeg"
            assert base64.b64decode(inline["data"]).startswith(bytes([255, 216]))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "candidates": [{"content": {"parts": [{"text": '{"typeTerms": ["bag"], "attributes": []}'}]}}]})
    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    result = await service._describe_image(service._image_data(image_body()))
    assert result["typeTerms"] == ["bag"]
