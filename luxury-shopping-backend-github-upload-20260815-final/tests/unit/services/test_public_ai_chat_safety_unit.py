from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.app.services import function_service as function_service_module
from backend.app.services.function_service import (
    _extract_gemini_answer,
    _gemini_answer,
    _gemini_model_candidates,
    _chat_site_context,
    _answer_matches_chat_intent,
    _customer_safe_site_context_v2,
    _extract_chat_budget,
    _fallback_chat_answer,
    _gemini_auth_header_options,
    _looks_like_unusable_ai_answer,
    _sanitize_public_chat_response,
)


def test_public_ai_chat_sanitizes_internal_catalog_leaks():
    leaked = """هذه أقرب معلومات مؤكدة من قاعدة الموقع:

عدد المنتجات المنشورة المتاحة للعميل: 84.
عدد المنتجات المطابقة لهذا السؤال: 0.
عدد المنتجات المخفضة النشطة: 56.

كوبونات فعالة:
- SAVEB5246319EB: Test coupon (100.00 ر.ي)
"""

    safe = _sanitize_public_chat_response(leaked, "ar")

    assert "عروض" in safe or "المنتجات" in safe
    assert "قاعدة الموقع" not in safe
    assert "84" not in safe
    assert "SAVEB5246319EB" not in safe
    assert "Test coupon" not in safe


def test_public_ai_chat_keeps_customer_visible_offer_prices():
    reply = "أقرب عروض مناسبة الآن: فستان فاخر — 58500.00 ر.ي بدل 76000.00 ر.ي. افتح صفحة العروض لتأكيد التوفر."

    safe = _sanitize_public_chat_response(reply, "ar")

    assert "فستان فاخر" in safe
    assert "58500.00 ر.ي" in safe
    assert "76000.00 ر.ي" in safe
    assert "قاعدة الموقع" not in safe


def test_public_ai_chat_keeps_safe_budget_rule_and_privacy_refusal():
    reply = "استخدم قاعدة 50/30/20 لتقسيم ميزانيتك. لا أستطيع مشاركة أسرار أو معلومات خاصة."

    safe = _sanitize_public_chat_response(reply, "ar")

    assert "قاعدة 50/30/20" in safe
    assert "لا أستطيع مشاركة أسرار" in safe


def test_public_ai_chat_keeps_safe_offer_heading_and_details():
    reply = "عروض نشطة الآن: حقيبة أنيقة بسعر 1850 ر.ي بدل 2000 ر.ي."

    safe = _sanitize_public_chat_response(reply, "ar")

    assert safe == reply


def test_public_ai_chat_requires_answer_to_match_customer_intent():
    assert _answer_matches_chat_intent(
        "كيف يتم الشحن؟",
        "تفاصيل الشحن تظهر أثناء إتمام الطلب.",
    )
    assert not _answer_matches_chat_intent(
        "كيف يتم الشحن؟",
        "مرحبًا، كيف أساعدك اليوم؟",
    )
    assert not _answer_matches_chat_intent(
        "كيف يتم الشحن والتتبع؟",
        "خيارات مناسبة: حقيبة أنيقة. افتح طلباتي للتتبع.",
    )
    assert _answer_matches_chat_intent(
        "ايش العروض؟",
        "عندنا عروض مختارة وأسعار مخفضة.",
    )


def test_public_ai_chat_detects_unusable_encoding_excuse():
    assert _looks_like_unusable_ai_answer(
        "يبدو أن نص رسالتك ظهر بلغة غير مفهومة أو رموز غير واضحة.",
        "ar",
    )
    assert not _looks_like_unusable_ai_answer(
        "تفاصيل الشحن تظهر أثناء إتمام الطلب بعد اختيار العنوان.",
        "ar",
    )


def test_public_ai_chat_rejects_and_sanitizes_internal_prompt_instruction():
    instruction = "هذا سؤال عام لا يحتاج بيانات المتجر. أجب عنه مباشرة من معرفتك العامة."

    assert _looks_like_unusable_ai_answer(instruction, "ar")
    assert instruction not in _sanitize_public_chat_response(instruction, "ar")


@pytest.mark.asyncio
async def test_public_ai_general_question_does_not_query_catalog():
    class NoQuerySession:
        async def execute(self, _statement):
            raise AssertionError("A general question must not query the catalog")

    context, has_products = await _chat_site_context(
        NoQuerySession(),
        "كيف أقسم ميزانيتي الشهرية بطريقة بسيطة؟",
        language="ar",
        user=None,
    )

    assert "سؤال عام" in context
    assert has_products is False


def test_public_ai_chat_keeps_safe_price_bullets_without_coupon_codes():
    reply = """اقتراحاتي الآن:
- فستان فاخر — 58500.00 ر.ي بدل 76000.00 ر.ي.
- SAVEB5246319EB: Test coupon (100.00 ر.ي)
"""

    safe = _sanitize_public_chat_response(reply, "ar")

    assert "فستان فاخر" in safe
    assert "58500.00 ر.ي" in safe
    assert "SAVEB5246319EB" not in safe
    assert "Test coupon" not in safe


def test_gemini_answer_extraction_ignores_internal_thought_parts():
    answer = _extract_gemini_answer(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": True, "text": "internal reasoning"},
                            {"text": "Complete customer answer."},
                        ]
                    }
                }
            ]
        }
    )

    assert answer == "Complete customer answer."


def test_public_ai_chat_extracts_arabic_budget_and_builds_gift_context():
    budget = _extract_chat_budget("ايش افضل هدية بحدود ٥٠٠٠٠")
    product = SimpleNamespace(
        name="فستان فاخر",
        name_en=None,
        promotional_title=None,
        price=Decimal("49000"),
        original_price=Decimal("52000"),
    )

    safe, has_products = _customer_safe_site_context_v2(
        message="ايش افضل هدية بحدود ٥٠٠٠٠",
        language="ar",
        products=[product],
        offers=[],
        categories=[],
        stores=[],
        orders=[],
        user=None,
        wants_offers=False,
        wants_categories=False,
        wants_stores=False,
        wants_orders=False,
        wants_shipping=False,
        wants_payment=False,
        wants_site_info=False,
        wants_gift=True,
        budget=budget,
        terms=[],
    )

    assert budget == Decimal("50000")
    assert has_products is True
    assert "فستان فاخر" in safe
    assert "49000.00 ر.ي" in safe
    assert "قاعدة الموقع" not in safe


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("أريد هدية بحدود خمسين ألف", Decimal("50000")),
        ("أريد هدية تحت 50 ألف", Decimal("50000")),
    ],
)
def test_public_ai_chat_extracts_arabic_thousands_budget(message, expected):
    assert _extract_chat_budget(message) == expected


def test_public_ai_fallback_uses_safe_gift_context():
    context = "أنسب هدايا بحدود 50000.00 ر.ي: فستان فاخر — 49000.00 ر.ي. اختَر حسب ذوق الشخص."

    answer = _fallback_chat_answer("ايش افضل هدية بحدود 50000", "ar", context)

    assert "فستان فاخر" in answer
    assert "49000.00 ر.ي" in answer
    assert "قاعدة الموقع" not in answer


def test_public_ai_fallback_uses_safe_offer_context_instead_of_generic_reply():
    context = "أقرب عروض مناسبة الآن: حقيبه كتف صغيره — 1850.00 ر.ي بدل 2000.00 ر.ي. افتح صفحة العروض لتأكيد التوفر قبل الطلب."

    answer = _fallback_chat_answer("ايش العروض", "ar", context)

    assert "حقيبه كتف صغيره" in answer
    assert "1850.00 ر.ي" in answer
    assert "اسألني بطريقتك" not in answer
    assert "عدد المنتجات" not in answer
    assert "قاعدة الموقع" not in answer


def test_public_ai_fallback_strips_sensitive_context_before_answering():
    context = """هذه أقرب معلومات مؤكدة من قاعدة الموقع:
عدد المنتجات المنشورة المتاحة للعميل: 84.
كوبونات فعالة:
- SAVEB5246319EB: Test coupon (100.00 ر.ي)
أقرب عروض مناسبة الآن: فستان فاخر — 49000.00 ر.ي بدل 52000.00 ر.ي."""

    answer = _fallback_chat_answer("ايش العروض", "ar", context)

    assert "فستان فاخر" in answer or "أقدر أساعدك" in answer
    assert "84" not in answer
    assert "SAVEB5246319EB" not in answer
    assert "Test coupon" not in answer
    assert "قاعدة الموقع" not in answer


def test_public_ai_gift_context_can_use_safe_offer_when_no_direct_products():
    budget = _extract_chat_budget("ايش افضل هدية بحدود 50000")
    offer = SimpleNamespace(
        name="حقيبة هدية",
        name_en=None,
        promotional_title=None,
        price=Decimal("1850"),
        original_price=Decimal("2000"),
    )

    safe, has_products = _customer_safe_site_context_v2(
        message="ايش افضل هدية بحدود 50000",
        language="ar",
        products=[],
        offers=[offer],
        categories=[],
        stores=[],
        orders=[],
        user=None,
        wants_offers=False,
        wants_categories=False,
        wants_stores=False,
        wants_orders=False,
        wants_shipping=False,
        wants_payment=False,
        wants_site_info=False,
        wants_gift=True,
        budget=budget,
        terms=[],
    )

    assert has_products is True
    assert "حقيبة هدية" in safe
    assert "1850.00 ر.ي" in safe
    assert "2000.00 ر.ي" in safe
    assert "قاعدة الموقع" not in safe


def test_public_ai_context_answers_site_purpose_without_internal_counts():
    safe, has_products = _customer_safe_site_context_v2(
        message="ايش يسوي الموقع",
        language="ar",
        products=[],
        offers=[],
        categories=[],
        stores=[],
        orders=[],
        user=None,
        wants_offers=False,
        wants_categories=False,
        wants_stores=False,
        wants_orders=False,
        wants_shipping=False,
        wants_payment=False,
        wants_site_info=True,
        wants_gift=False,
        budget=None,
        terms=[],
    )

    assert has_products is False
    assert "رفاهية التسوق" in safe
    assert "تتابع الشحن" in safe
    assert "قاعدة الموقع" not in safe
    assert "عدد المنتجات" not in safe


def test_public_ai_context_answers_shipping_details_directly():
    safe, has_products = _customer_safe_site_context_v2(
        message="تفاصيل الشحن",
        language="ar",
        products=[],
        offers=[],
        categories=[],
        stores=[],
        orders=[],
        user=None,
        wants_offers=False,
        wants_categories=False,
        wants_stores=False,
        wants_orders=False,
        wants_shipping=True,
        wants_payment=False,
        wants_site_info=False,
        wants_gift=False,
        budget=None,
        terms=[],
    )

    assert has_products is False
    assert "تفاصيل الشحن" in safe
    assert "إتمام الطلب" in safe
    assert "قاعدة الموقع" not in safe


def test_public_ai_context_prioritizes_shipping_when_tracking_is_requested_too():
    safe, has_products = _customer_safe_site_context_v2(
        message="كيف يتم الشحن والتتبع؟",
        language="ar",
        products=[],
        offers=[],
        categories=[],
        stores=[],
        orders=[],
        user=None,
        wants_offers=False,
        wants_categories=False,
        wants_stores=False,
        wants_orders=True,
        wants_shipping=True,
        wants_payment=False,
        wants_site_info=False,
        wants_gift=False,
        budget=None,
        terms=[],
    )

    assert has_products is False
    assert "تفاصيل الشحن" in safe
    assert "طلباتي" in safe


def test_public_ai_fallback_does_not_return_product_context_for_shipping():
    context = "خيارات مناسبة: حقيبة أنيقة — 1850.00 ر.ي. افتح طلباتي للتتبع."

    answer = _fallback_chat_answer("كيف يتم الشحن والتتبع؟", "ar", context)

    assert "تفاصيل الشحن" in answer
    assert "حقيبة أنيقة" not in answer


def test_public_ai_fallback_never_exposes_general_question_instruction():
    context = "هذا سؤال عام لا يحتاج بيانات المتجر. أجب عنه مباشرة من معرفتك العامة."

    answer = _fallback_chat_answer("كيف أقسم ميزانيتي الشهرية؟", "ar", context)

    assert "هذا سؤال عام" not in answer
    assert "50/30/20" in answer


def test_public_ai_fallback_answers_site_services_without_internal_instruction():
    context = "هذا سؤال عام لا يحتاج بيانات المتجر. أجب عنه مباشرة من معرفتك العامة."

    answer = _fallback_chat_answer("ايش الخدمات التي يقدمها الموقع؟", "ar", context)

    assert "رفاهية التسوق" in answer
    assert "تتابع الشحن" in answer
    assert "هذا سؤال عام" not in answer


def test_gemini_auth_header_options_uses_api_key_header_for_ai_studio_key():
    options = _gemini_auth_header_options("AQ.sample-token")

    assert options == [
        {
            "x-goog-api-key": "AQ.sample-token",
            "Content-Type": "application/json",
        }
    ]


def test_gemini_auth_header_options_uses_bearer_only_for_oauth_access_token():
    options = _gemini_auth_header_options("ya29.sample-token")

    assert options[0]["Authorization"] == "Bearer ya29.sample-token"
    assert options[0]["Content-Type"] == "application/json"
    assert "x-goog-api-key" not in options[0]


def test_gemini_auth_header_options_prefers_api_key_for_google_api_key():
    options = _gemini_auth_header_options("AIzaSampleKey")

    assert options[0]["x-goog-api-key"] == "AIzaSampleKey"
    assert options[0]["Content-Type"] == "application/json"
    assert len(options) == 1


def test_gemini_model_candidates_skip_retired_models(monkeypatch: pytest.MonkeyPatch):
    settings = SimpleNamespace(
        ai_default_model="gemini-2.0-flash",
        ai_model_allowlist="gemini-2.0-flash-001,gemini-2.5-flash",
    )
    monkeypatch.setattr(function_service_module, "get_settings", lambda: settings)

    candidates = _gemini_model_candidates()

    assert candidates[0] == "gemini-2.5-flash"
    assert "gemini-3.6-flash" in candidates
    assert "gemini-2.0-flash" not in candidates
    assert "gemini-2.0-flash-001" not in candidates


@pytest.mark.asyncio
async def test_gemini_answer_uses_current_model_and_safe_api_key_header(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        ai_default_model="gemini-2.0-flash",
        ai_model_allowlist="",
        resolved_ai_api_key="AQ.sample-token",
        ai_api_url="",
        ai_request_timeout_seconds=2,
        ai_max_output_tokens=256,
    )
    calls: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "AI response"}]}}
                ]
            }

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(function_service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(function_service_module.httpx, "AsyncClient", FakeAsyncClient)

    answer = await _gemini_answer(
        [
            {"role": "system", "content": "Stay safe."},
            {"role": "user", "content": "Recommend a gift."},
        ]
    )

    assert answer == "AI response"
    assert len(calls) == 1
    assert "gemini-3.6-flash" in calls[0]["url"]
    assert calls[0]["headers"]["x-goog-api-key"] == "AQ.sample-token"
    assert "Authorization" not in calls[0]["headers"]
    assert "temperature" not in calls[0]["json"]["generationConfig"]
    assert calls[0]["json"]["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "low"
    }


@pytest.mark.asyncio
async def test_gemini_answer_tries_all_safe_models_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        ai_default_model="gemini-3.6-flash",
        ai_model_allowlist="gemini-2.5-flash",
        resolved_ai_api_key="AQ.sample-token",
        ai_api_url="",
        ai_request_timeout_seconds=2,
        ai_max_output_tokens=256,
    )
    calls: list[str] = []

    class FakeResponse:
        status_code = 429

        def raise_for_status(self) -> None:
            request = function_service_module.httpx.Request("POST", "https://example.test")
            response = function_service_module.httpx.Response(429, request=request)
            raise function_service_module.httpx.HTTPStatusError(
                "rate limited",
                request=request,
                response=response,
            )

        def json(self) -> dict:
            return {}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(function_service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(function_service_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(Exception) as exc_info:
        await _gemini_answer([{"role": "user", "content": "Hello"}])

    assert getattr(exc_info.value, "detail", {}).get("code") == "ai_provider_rate_limited"
    assert len(calls) == 4
    assert "gemini-3.6-flash" in calls[0]
    assert "gemini-2.5-flash-lite" in calls[1]
    assert "gemini-2.5-flash" in calls[2]
    assert "gemini-3.5-flash" in calls[3]


@pytest.mark.asyncio
async def test_gemini_answer_returns_lite_fallback_answer_after_primary_quota_error(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        ai_default_model="gemini-3.6-flash",
        ai_model_allowlist="gemini-2.5-flash",
        resolved_ai_api_key="AQ.sample-token",
        ai_api_url="",
        ai_request_timeout_seconds=2,
        ai_max_output_tokens=256,
    )
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code != 429:
                return
            request = function_service_module.httpx.Request("POST", "https://example.test")
            response = function_service_module.httpx.Response(429, request=request)
            raise function_service_module.httpx.HTTPStatusError(
                "rate limited",
                request=request,
                response=response,
            )

        def json(self) -> dict:
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "Helpful AI answer"}]}}
                ]
            }

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            calls.append(url)
            return FakeResponse(429 if len(calls) == 1 else 200)

    monkeypatch.setattr(function_service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(function_service_module.httpx, "AsyncClient", FakeAsyncClient)

    answer = await _gemini_answer([{"role": "user", "content": "Hello"}])

    assert answer == "Helpful AI answer"
    assert len(calls) == 2
    assert "gemini-3.6-flash" in calls[0]
    assert "gemini-2.5-flash-lite" in calls[1]
