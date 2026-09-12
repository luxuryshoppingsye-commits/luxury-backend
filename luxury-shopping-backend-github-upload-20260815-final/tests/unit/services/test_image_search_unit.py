import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageOps

from backend.app.services import image_search as service


def image_body():
    data = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(data, "PNG")
    return {"imageBase64": "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()}


def colored_image_body(color):
    data = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(data, "PNG")
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


def test_gemini_json_parser_accepts_fenced_response():
    payload = {
        "candidates": [{
            "content": {
                "parts": [{"text": '```json\n{"typeTerms": ["bag"], "attributes": []}\n```'}],
            },
        }],
    }
    assert service._json_object_from_gemini(payload) == {
        "typeTerms": ["bag"],
        "attributes": [],
    }


def test_image_model_candidates_skip_retired_and_generator_models():
    settings = SimpleNamespace(
        ai_default_model="gemini-2.0-flash",
        ai_model_allowlist="gemini-2.5-flash-image,gemini-2.5-flash",
    )
    assert service._gemini_image_model_candidates(settings) == [
        "gemini-2.5-flash",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-flash-latest",
    ]


@pytest.mark.asyncio
async def test_model_discovery_keeps_only_generate_content_gemini_models():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "models": [
                    {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/text-embedding-005", "supportedGenerationMethods": ["embedContent"]},
                ]
            }

    class Client:
        async def get(self, url, headers):
            assert url.endswith("/v1beta/models")
            assert headers["x-goog-api-key"] == "test-key"
            return Response()

    assert await service._discover_gemini_image_models(
        Client(), {"x-goog-api-key": "test-key"}
    ) == ["gemini-3.6-flash"]


@pytest.mark.asyncio
async def test_image_analysis_discovers_a_provider_model_after_static_404s(monkeypatch):
    settings = SimpleNamespace(
        gemini_api_key="test-key",
        google_api_key="",
        ai_api_key="",
        ai_api_url="",
        ai_default_model="gemini-2.0-flash",
        ai_model_allowlist="",
        ai_request_timeout_seconds=2,
    )
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            calls.append(("post", url))
            if "gemini-discovered" not in url:
                return httpx.Response(404, request=httpx.Request("POST", url))
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "candidates": [{"content": {"parts": [
                    {"text": '{"typeTerms": ["bag"], "attributes": []}'},
                ]}}],
            })

        async def get(self, url, *, headers):
            calls.append(("get", url))
            return httpx.Response(200, request=httpx.Request("GET", url), json={
                "models": [{
                    "name": "models/gemini-discovered",
                    "supportedGenerationMethods": ["generateContent"],
                }],
            })

    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = await service._describe_image("encoded-image")

    assert result["typeTerms"] == ["bag"]
    assert any(kind == "get" for kind, _ in calls)
    assert calls[-1][1].endswith("/models/gemini-discovered:generateContent")


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
async def test_visual_search_does_not_require_product_name_match(monkeypatch):
    query_body = colored_image_body((218, 170, 40))
    query_encoded = service._image_data(query_body)
    query_signature = service._visual_signature(query_encoded)
    wrong_signature = service._visual_signature(service._image_data(colored_image_body((30, 80, 190))))

    monkeypatch.setattr(service, "_describe_image", AsyncMock(return_value={
        "productType": "", "typeTerms": [], "attributes": []}))
    monkeypatch.setattr(service, "_product_image_refs", lambda product: [product.image_url])

    async def signature_for(ref):
        return query_signature if ref == "match" else wrong_signature

    monkeypatch.setattr(service, "_cached_product_image_signature", signature_for)
    monkeypatch.setattr(service, "serialize_record", lambda product: {"id": product.id, "name": product.name})
    rows = [
        SimpleNamespace(id="wrong", name="منتج باسم مختلف", image_url="other"),
        SimpleNamespace(id="match", name="اسم لا يذكر نوع المنتج", image_url="match"),
    ]

    class Session:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: rows)

    result = await service.search_catalog_image(query_body, Session())

    assert result["searchInfo"]["source"] == "visual_image_similarity"
    assert result["products"][0]["id"] == "match"


def test_visual_similarity_accepts_a_reframed_product_photo():
    source = Image.new("RGB", (240, 360), (242, 242, 242))
    draw = ImageDraw.Draw(source)
    draw.rounded_rectangle((60, 45, 180, 315), radius=24, fill=(218, 170, 40))
    draw.ellipse((88, 120, 152, 184), fill=(245, 220, 120))
    reframed = ImageOps.pad(source, (360, 360), method=Image.Resampling.LANCZOS, color="white")

    def signature(image):
        output = io.BytesIO()
        image.save(output, "PNG")
        return service._visual_signature(base64.b64encode(output.getvalue()).decode())

    assert service._visual_similarity(signature(source), signature(reframed)) >= service._VISUAL_MATCH_THRESHOLD


def test_product_image_refs_accept_path_objects():
    product = SimpleNamespace(
        image_url=None,
        images=[{"path": "/uploads/products/catalog-item.webp"}],
    )

    assert service._product_image_refs(product) == ["/uploads/products/catalog-item.webp"]


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


@pytest.mark.asyncio
async def test_provider_falls_back_to_available_model_and_fenced_json(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(
        gemini_api_key="test-key", google_api_key="", ai_api_key="",
        ai_default_model="gemini-2.5-flash", ai_model_allowlist="gemini-2.5-flash",
        ai_request_timeout_seconds=10))
    calls = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json):
            calls.append(url)
            if len(calls) == 1:
                response = httpx.Response(404, request=httpx.Request("POST", url))
                raise httpx.HTTPStatusError("model unavailable", request=response.request, response=response)
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"candidates": [{"content": {"parts": [
                    {"text": '```json\n{"typeTerms": ["bag"], "attributes": []}\n```'},
                ]}}]},
            )

    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    result = await service._describe_image(service._image_data(image_body()))
    assert result["typeTerms"] == ["bag"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_product_description_is_grounded_in_the_actual_product_image(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(
        gemini_api_key="test-key", google_api_key="", ai_api_key="", ai_default_model="gemini-2.5-flash", ai_request_timeout_seconds=10))
    encoded = service._image_data(image_body())
    monkeypatch.setattr(service, "_image_data_from_public_url", AsyncMock(return_value=encoded))

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json):
            assert "actual product image" in json["contents"][0]["parts"][0]["text"]
            inline = json["contents"][0]["parts"][1]["inlineData"]
            assert inline["mimeType"] == "image/jpeg"
            assert base64.b64decode(inline["data"]).startswith(bytes([255, 216]))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "candidates": [{"content": {"parts": [{"text": '{"description": "حقيبة حمراء صغيرة بتصميم بسيط ومقبض علوي واضح في الصورة، مناسبة للاستخدام اليومي وحمل الأغراض الأساسية.", "tags": ["حقيبة", "حمراء"]}'}]}}]})

    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    result = await service.describe_product_image("https://images.example.test/product.jpg", "حقيبة")
    assert result["description"].startswith("حقيبة حمراء")
    assert result["tags"] == ["حقيبة", "حمراء"]


@pytest.mark.asyncio
async def test_product_description_rejects_non_https_image_urls():
    with pytest.raises(HTTPException) as exc:
        await service._assert_public_https_url("http://example.test/product.jpg")
    assert exc.value.status_code == 422
