from __future__ import annotations

import secrets
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import Category, LoginAttempt, Order, OrderItem, Product, Profile, User, UserRole
from ..repositories.resources import serialize_record
from ..security.passwords import hash_password
from .api_protection import (
    AI_GENERATION_FUNCTIONS,
    AI_RECOMMENDATION_FUNCTIONS,
    AIQuotaService,
    current_request_id,
)
from .auth_service import account_security_for, bump_security_version, roles_for
from .catalog_policy import public_product_clauses
from .financial_calculator import money
from .outbox_service import process_email_outbox
from .notification_service import NotificationPayload, NotificationService


STAFF_ROLES = {"admin", "manager", "finance", "logistics", "staff", "employee"}
ADMIN_ROLES = {"admin", "manager"}
RATE_LIMIT_ENUMERATION_FUNCTIONS = {"check_login_rate_limit", "check_password_reset_rate_limit", "is_identity_banned"}


def _uuid(value: Any, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"invalid_uuid:{field}")


def _text(body: dict[str, Any], *names: str, default: str = "") -> str:
    for name in names:
        value = body.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _normalize_chat_text(value: str) -> str:
    return (
        value.strip()
        .lower()
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ٱ", "ا")
        .replace("ة", "ه")
        .replace("ى", "ي")
        .replace("ؤ", "و")
        .replace("ئ", "ي")
    )


def _has_any_chat_term(value: str, terms: list[str]) -> bool:
    normalized = _normalize_chat_text(value)
    return any(_normalize_chat_text(term) in normalized for term in terms)


def _has_any_whole_chat_term(value: str, terms: list[str]) -> bool:
    normalized = _normalize_chat_text(value)
    return any(
        re.search(rf"(?<!\w){re.escape(_normalize_chat_text(term))}(?!\w)", normalized)
        for term in terms
    )


def _chat_direct_guidance(message: str, language: str) -> str | None:
    """Return precise app guidance before catalog retrieval or model generation."""
    english = language == "en"
    add_to_cart = (
        _has_any_chat_term(
            message,
            [
                "اضيف للسلة",
                "أضيف للسلة",
                "اضف للسلة",
                "أضف للسلة",
                "add to cart",
                "اضافة للسلة",
            ],
        )
        or (
            _has_any_chat_term(message, ["اضيف", "اضف", "أضيف", "أضف", "add"])
            and _has_any_chat_term(message, ["سلة", "السلة", "عربة", "عربه", "cart"])
        )
    )
    remove_from_cart = (
        _has_any_chat_term(
            message,
            [
                "احذف من السلة",
                "أحذف من السلة",
                "ازيل من السلة",
                "أزيل من السلة",
                "شيل من السلة",
                "remove from cart",
                "delete from cart",
            ],
        )
        or (
            _has_any_chat_term(message, ["احذف", "أحذف", "ازيل", "أزيل", "شيل", "remove", "delete"])
            and _has_any_chat_term(message, ["سلة", "السلة", "عربة", "عربه", "cart"])
        )
    )
    add_to_wishlist = (
        _has_any_chat_term(
            message,
            [
                "اضيف للمفضلة",
                "أضيف للمفضلة",
                "اضف للمفضلة",
                "أضف للمفضلة",
                "add to wishlist",
                "add to favorites",
            ],
        )
        or (
            _has_any_chat_term(message, ["اضيف", "اضف", "أضيف", "أضف", "add"])
            and _has_any_chat_term(message, ["مفضلة", "المفضلة", "امنيات", "أمنيات", "wishlist", "favorites"])
        )
    )
    remove_from_wishlist = (
        _has_any_chat_term(
            message,
            [
                "احذف من المفضلة",
                "أحذف من المفضلة",
                "ازيل من المفضلة",
                "أزيل من المفضلة",
                "remove from wishlist",
                "remove from favorites",
            ],
        )
        or (
            _has_any_chat_term(message, ["احذف", "أحذف", "ازيل", "أزيل", "شيل", "remove", "delete"])
            and _has_any_chat_term(message, ["مفضلة", "المفضلة", "امنيات", "أمنيات", "wishlist", "favorites"])
        )
    )

    if add_to_cart:
        return (
            "Open the product, choose an available color or size, set the quantity, then tap Add to cart."
            if english
            else "افتح المنتج، اختر اللون أو المقاس المتاح، حدد الكمية، ثم اضغط إضافة للسلة."
        )
    if remove_from_cart:
        return (
            "Open the Cart tab, find the item, then use its remove or delete control."
            if english
            else "افتح تبويب السلة، ابحث عن المنتج، ثم استخدم زر الحذف أو الإزالة."
        )
    if add_to_wishlist:
        return (
            "Tap the heart on the product card to save it to your Wishlist."
            if english
            else "اضغط أيقونة القلب في بطاقة المنتج لإضافته إلى المفضلة."
        )
    if remove_from_wishlist:
        return (
            "Open Wishlist and tap the filled heart on the product to remove it."
            if english
            else "افتح المفضلة واضغط القلب المعبأ على المنتج لإزالته."
        )
    if _has_any_chat_term(
        message,
        ["اضافه منتج", "إضافة منتج", "اضيف منتج", "أضيف منتج", "اضف منتج", "أضف منتج", "add product"],
    ):
        return (
            "Open the product details, choose the available options, then tap Add to cart."
            if english
            else "لإضافة منتج للشراء، افتح تفاصيله، اختر الخيارات المتاحة، ثم اضغط إضافة للسلة."
        )

    normalized = _normalize_chat_text(message)
    if _has_any_chat_term(
        message,
        ["واقع معزز", "الواقع المعزز", "تجربة المنتج", "try-on", "try on", "augmented reality"],
    ) or bool(re.search(r"\bar\b", normalized)):
        return (
            "Open product details and tap AR try-on when available. Point the camera at the body, hand, or foot, then move and resize the item."
            if english
            else "افتح تفاصيل المنتج ثم اضغط تجربة الواقع المعزز إذا كانت متاحة. وجّه الكاميرا للجسم أو اليد أو القدم حسب نوع المنتج، وبعدها حرّك المنتج وعدّل حجمه."
        )
    if _has_any_chat_term(message, ["تتبع الطلب", "طلباتي", "رقم الطلب", "tracking", "my orders"]):
        return (
            "Sign in, open My Orders, choose the order, and check its status and tracking updates."
            if english
            else "سجّل دخولك، افتح طلباتي، اختر الطلب، ثم راجع حالته وتحديثات التتبع."
        )
    if _has_any_chat_term(message, ["شراء", "اشتري", "أشتري", "اكمل الطلب", "إتمام الطلب", "checkout", "buy now"]):
        return (
            "Open the product, choose its options, add it to cart, then sign in and complete checkout with your address and payment method."
            if english
            else "افتح المنتج، اختر خياراته، أضفه للسلة، ثم سجّل دخولك وأكمل الطلب بالعنوان وطريقة الدفع."
        )
    if _has_any_chat_term(message, ["طرق الدفع", "طريقة الدفع", "دفع", "تحويل", "payment", "receipt"]):
        return (
            "Payment methods appear during checkout. If you choose a transfer, upload the receipt from the order page."
            if english
            else "طرق الدفع تظهر أثناء إتمام الطلب. إذا اخترت التحويل، ارفع الإيصال من صفحة الطلب."
        )
    if _has_any_chat_term(message, ["الشحن", "شحن", "التوصيل", "توصيل", "shipping", "delivery"]):
        return (
            "Shipping cost and delivery time appear during checkout based on the address and delivery method."
            if english
            else "تكلفة الشحن ووقت الوصول يظهران أثناء إتمام الطلب حسب العنوان وطريقة التوصيل."
        )
    if _has_any_chat_term(
        message,
        [
            "ارجاع", "ارجع", "إرجاع", "استبدال", "استبدل", "الغاء طلب", "إلغاء طلب",
            "refund", "return", "exchange", "cancel order",
        ],
    ):
        return (
            "Open the order from My Orders and contact support to request a return or exchange. Keep the order number and product details ready."
            if english
            else "افتح الطلب من طلباتي وتواصل مع الدعم لطلب الإرجاع أو الاستبدال. جهّز رقم الطلب وتفاصيل المنتج."
        )
    if _has_any_chat_term(message, ["مقارنة", "قارن", "مقارنه", "compare"]):
        return (
            "Open the product card, tap Compare, select the other products, then open the comparison page to review the details."
            if english
            else "افتح بطاقة المنتج واضغط مقارنة، اختر المنتجات الأخرى، ثم افتح صفحة المقارنة لمراجعة التفاصيل."
        )
    if _has_any_chat_term(message, ["مشاركة", "شارك", "مشاركه", "share"]):
        return (
            "Open the product card and tap Share to send its product link."
            if english
            else "افتح بطاقة المنتج واضغط مشاركة لإرسال رابط المنتج."
        )
    return None


def _fallback_chat_answer(message: str, language: str = "ar", context: str = "") -> str:
    english = language == "en"
    if _has_any_chat_term(
        message,
        [
            "قاعدة البيانات", "رابط قاعدة", "مفتاح سري", "كلمة مرور", "jwt",
            "secret", "token", "database url", "api key", "password",
        ],
    ):
        return (
            "I cannot share secrets, private identifiers, database links, or internal statistics. I can still help with public products, offers, orders, and support."
            if english
            else "ما أقدر أشارك أسرارًا أو معرفات خاصة أو روابط قواعد بيانات أو إحصاءات داخلية. أقدر أساعدك بأمان في المنتجات والعروض والطلبات والدعم."
        )
    direct_guidance = _chat_direct_guidance(message, language)
    if direct_guidance:
        return direct_guidance
    safe_context = _sanitize_public_chat_response(context, language) if context.strip() else ""
    if (
        safe_context
        and not _is_public_chat_instruction(safe_context)
        and _looks_like_specific_customer_answer(safe_context, language)
        and _answer_matches_chat_intent(message, safe_context)
    ):
        return safe_context
    if _has_any_chat_term(message, ["مرحبا", "هلا", "اهلا", "السلام", "hello", "hi"]):
        return (
            "Hello. I am Noura, with you now. Ask me about products, offers, orders, cart, payment, delivery, returns, or any general question."
            if english
            else "يا هلا وسهلاً، أنا نورة معك الآن. اسألني عن المنتجات أو العروض أو الطلبات أو السلة أو الدفع أو التوصيل أو الإرجاع، وحتى أي سؤال عام بجاوبك بوضوح."
        )
    if _has_any_chat_term(message, ["ايش يسوي الموقع", "وش يسوي الموقع", "ما هو الموقع", "عن الموقع", "خدمات الموقع", "خدمات", "يقدمها الموقع", "what does the site do", "what is this site", "what services"]):
        return (
            "Luxury Shopping helps you browse products, offers, stores, cart, checkout, payment, delivery tracking, and returns from one place."
            if english
            else "رفاهية التسوق منصة تجمع لك المنتجات والعروض والمتاجر في مكان واحد. تقدر تبحث، تقارن، تضيف للسلة، تكمل الطلب، وتتابع الشحن من حسابك."
        )
    if _has_any_chat_term(message, ["الشحن", "شحن", "تفاصيل الشحن", "معلومات الشحن", "طريقة الشحن", "تكلفة الشحن", "مدة الشحن", "التوصيل", "shipping", "shipping details", "delivery details", "shipping cost"]):
        return (
            "Shipping details appear at checkout after choosing the address and delivery option. After ordering, track updates from My Orders."
            if english
            else "تفاصيل الشحن تظهر أثناء إتمام الطلب بعد اختيار العنوان وطريقة التوصيل. بعد إنشاء الطلب تقدر تتابع الحالة والتحديثات من طلباتي."
        )
    if _has_any_chat_term(message, ["عرض", "عروض", "خصم", "كوبون", "coupon", "discount", "offer"]):
        return (
            "Open the Offers page to see discounted products. If you have a coupon, enter it during checkout."
            if english
            else "نعم، توجد عروض مختارة. افتح صفحة العروض للخصومات الحالية، أو اكتب القسم أو الميزانية وبأرشح لك خيارات مخفضة مناسبة."
        )
    if _has_any_chat_term(
        message,
        [
            "اضيف للسلة", "أضيف للسلة", "اضف للسلة", "أضف للسلة",
            "add to cart", "اضافة للسلة",
        ],
    ):
        return (
            "Open the product, choose an available color or size, set the quantity, then tap Add to cart."
            if english
            else "افتح المنتج، اختر اللون أو المقاس المتاح، حدد الكمية، ثم اضغط إضافة للسلة."
        )
    if _has_any_chat_term(
        message,
        [
            "احذف من السلة", "أحذف من السلة", "ازيل من السلة", "أزيل من السلة",
            "شيل من السلة", "remove from cart", "delete from cart",
        ],
    ):
        return (
            "Open the Cart tab, find the item, and use its remove or delete control."
            if english
            else "افتح تبويب السلة، ابحث عن المنتج، ثم استخدم زر الحذف أو الإزالة."
        )
    if _has_any_chat_term(
        message,
        [
            "اضيف للمفضلة", "أضيف للمفضلة", "اضف للمفضلة", "أضف للمفضلة",
            "add to wishlist", "add to favorites",
        ],
    ):
        return (
            "Tap the heart on the product card to save it to your Wishlist."
            if english
            else "اضغط أيقونة القلب في بطاقة المنتج لإضافته إلى المفضلة."
        )
    if _has_any_chat_term(
        message,
        [
            "احذف من المفضلة", "أحذف من المفضلة", "ازيل من المفضلة", "أزيل من المفضلة",
            "remove from wishlist", "remove from favorites",
        ],
    ):
        return (
            "Open Wishlist and tap the filled heart on the product to remove it."
            if english
            else "افتح المفضلة واضغط القلب المعبأ على المنتج لإزالته."
        )
    if _has_any_chat_term(
        message,
        [
            "مميز", "مميزه", "الافضل", "الأفضل", "الاكثر طلبا",
            "featured", "best product", "best products",
        ],
    ):
        safe_context = _sanitize_public_chat_response(context, language) if context.strip() else ""
        if safe_context and _answer_matches_chat_intent(message, safe_context):
            return safe_context
        return (
            "Open Featured Products on the home page to browse the store's highlighted picks."
            if english
            else "افتح قسم المنتجات المميزة في الرئيسية لمشاهدة اختيارات المتجر المميزة."
        )
    if _has_any_chat_term(message, ["منتج", "بحث", "شنطه", "حقيبه", "فستان", "ساعه", "حذاء", "product", "search", "bag", "dress", "watch", "shoe"]):
        return (
            "Tell me the product name, category, or brand and I will guide you to the best way to find it."
            if english
            else "قل لي اسم المنتج أو القسم أو الماركة التي تبحث عنها، وبأرشدك لأقرب نتيجة أو فلتر مناسب."
        )
    if _has_any_chat_term(message, ["طلب", "تتبع", "وصل", "order", "tracking"]):
        return (
            "For tracking, open My Orders, choose the order, and check its status and tracking updates."
            if english
            else "لتتبع الطلب افتح حسابي ثم طلباتي، اختر الطلب، وستظهر لك الحالة ورقم التتبع والتحديثات."
        )
    if _has_any_chat_term(message, ["سله", "سلة", "cart"]):
        return (
            "Open the cart icon, review quantities, then continue to checkout."
            if english
            else "افتح أيقونة السلة، راجع المنتجات والكميات، ثم اضغط إتمام الطلب."
        )
    if _has_any_chat_term(message, ["دفع", "ايصال", "فاتوره", "payment", "receipt", "invoice"]):
        return (
            "Payment methods appear during checkout. For transfer payments, upload the receipt from the order page."
            if english
            else "طرق الدفع تظهر أثناء إتمام الطلب. إذا اخترت التحويل، ارفع الإيصال من صفحة الطلب حتى تتم مراجعته."
        )
    if _has_any_chat_term(message, ["ميزانية", "ميزانيتي", "تقسيم الميزانية", "budget"]):
        return (
            "A simple starting point is the 50/30/20 rule: 50% for essentials, 30% for flexible spending, and 20% for saving or debt. Adjust it to your income and fixed commitments."
            if english
            else "قاعدة بسيطة للبدء هي 50/30/20: خصص 50% للأساسيات، و30% للمصروف المرن، و20% للادخار أو سداد الالتزامات. عدّل النسب حسب دخلك ومصاريفك الثابتة."
        )
    return (
        "I am with you. Ask naturally and I will answer briefly, then guide you to the right shopping step if needed."
        if english
        else "أنا معك. اسألني بطريقتك حتى لو السؤال مش عن المتجر مباشرة؛ بجاوبك باختصار وبعدين أرشدك للخطوة المناسبة إذا احتجت."
    )


def _require_user(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="authentication_required")
    return user


def _require_roles(roles: set[str], allowed: set[str]) -> None:
    if not roles.intersection(allowed):
        raise HTTPException(status_code=403, detail="insufficient_permissions")


async def _coupon_payload(
    session: AsyncSession,
    body: dict[str, Any],
    user: User,
) -> dict[str, Any]:
    coupon_model = MODEL_BY_TABLE["coupons"]
    code = _text(body, "code", "p_code").upper()
    coupon_id = body.get("coupon_id") or body.get("p_coupon_id")
    statement = select(coupon_model).where(coupon_model.deleted_at.is_(None))
    if coupon_id:
        statement = statement.where(coupon_model.id == _uuid(coupon_id, "coupon_id"))
    elif code:
        statement = statement.where(func.upper(coupon_model.code) == code)
    else:
        raise HTTPException(status_code=400, detail="coupon_required")
    coupon = (await session.execute(statement.with_for_update())).scalar_one_or_none()
    if coupon is None or not coupon.is_active:
        return {"valid": False, "reason": "coupon_not_found"}
    if coupon.expires_at and coupon.expires_at <= datetime.now(timezone.utc):
        return {"valid": False, "reason": "coupon_expired"}
    usage_model = MODEL_BY_TABLE["coupon_usage"]
    prior = await session.execute(
        select(func.count())
        .select_from(usage_model)
        .where(
            usage_model.user_id == user.id,
            usage_model.extra_data["coupon_id"].astext == str(coupon.id),
            usage_model.deleted_at.is_(None),
        )
    )
    extra = dict(coupon.extra_data or {})
    per_user = int(extra.get("uses_per_user") or 1)
    if int(prior.scalar_one()) >= per_user:
        return {"valid": False, "reason": "coupon_usage_limit"}
    subtotal = Decimal(str(body.get("subtotal") or body.get("p_subtotal") or 0))
    minimum = Decimal(str(extra.get("min_order_amount") or 0))
    if subtotal < minimum:
        return {"valid": False, "reason": "minimum_order_not_met"}
    discount_type = str(extra.get("discount_type") or "fixed")
    discount_value = Decimal(str(extra.get("discount_value") or coupon.amount or 0))
    if discount_type == "percentage":
        discount = subtotal * discount_value / Decimal("100")
    elif discount_type == "free_shipping":
        discount = Decimal("0")
    else:
        discount = discount_value
    discount = min(max(discount, Decimal("0")), subtotal)
    return {
        **serialize_record(coupon),
        "valid": True,
        "discount_type": discount_type,
        "discount_value": str(money(discount_value)),
        "discount_amount": str(money(discount)),
        "discountAmount": str(money(discount)),
        "free_shipping": discount_type == "free_shipping",
    }


async def _queue_message(
    session: AsyncSession,
    table: str,
    *,
    user_id: uuid.UUID | None,
    title: str,
    message: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    model = MODEL_BY_TABLE[table]
    dedupe_seed = f"{table}:{user_id}:{title}:{body.get('application_id') or body.get('order_id') or body.get('template') or ''}"
    values: dict[str, Any] = {
        "user_id": user_id,
        "title": title,
        "message": message,
        "status": "queued",
        "extra_data": {
            "category": body.get("category") or "system",
            "template": body.get("template") or "server_template",
            "dedupe_key": body.get("dedupe_key") or hashlib.sha256(dedupe_seed.encode("utf-8")).hexdigest(),
            **{key: value for key, value in body.items() if key not in {"subject", "message", "body", "raw_html", "from", "reply_to", "cc", "bcc"}},
        },
    }
    if table == "email_outbox":
        values["email"] = _text(body, "email", "to") or None
    if table == "whatsapp_outbox":
        values["phone"] = _text(body, "phone", "to") or None
    row = model(**values)
    session.add(row)
    await session.flush()
    return serialize_record(row)


def _ai_provider_kind() -> str:
    provider = get_settings().ai_provider_name.strip().lower()
    if "gemini" in provider or "google" in provider:
        return "gemini"
    return provider or "openai_compatible"


def _gemini_model() -> str:
    configured = get_settings().ai_default_model.strip()
    if not configured or configured == "default":
        return "gemini-3.6-flash"
    return configured


def _gemini_model_candidates() -> list[str]:
    settings = get_settings()
    configured = _gemini_model()
    allowlist = [
        item.strip()
        for item in str(settings.ai_model_allowlist or "").split(",")
        if item.strip()
    ]
    retired_models = {
        "gemini-1.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
    }
    candidates: list[str] = []
    for model in [
        configured,
        *allowlist,
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ]:
        normalized = model.strip()
        if (
            normalized
            and normalized != "default"
            and normalized not in retired_models
            and normalized not in candidates
        ):
            candidates.append(normalized)
    if "gemini-2.5-flash-lite" not in candidates:
        candidates.insert(1 if candidates else 0, "gemini-2.5-flash-lite")
    return candidates


def _extract_gemini_answer(payload: dict[str, Any]) -> str | None:
    for key in ("answer", "output_text", "text", "response"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    output = payload.get("output")
    if isinstance(output, str) and output.strip():
        return output.strip()
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(part.strip() for part in parts if part.strip())
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates:
        parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
        if isinstance(parts, list):
            text_parts = [
                str(part.get("text") or "").strip()
                for part in parts
                if isinstance(part, dict) and not part.get("thought")
            ]
            answer = "\n".join(part for part in text_parts if part)
            if answer:
                return answer
    return None


def _gemini_auth_header_options(api_key: str) -> list[dict[str, str]]:
    secret = api_key.strip()
    if not secret:
        return []
    content_type = {"Content-Type": "application/json"}
    if secret.startswith("ya29."):
        return [{"Authorization": f"Bearer {secret}", **content_type}]
    return [{"x-goog-api-key": secret, **content_type}]


def _conversation_as_text(messages: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role") or "user"
        content = (message.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


async def _gemini_answer(messages: list[dict[str, str]]) -> str:
    settings = get_settings()
    api_key = settings.resolved_ai_api_key
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={"code": "ai_provider_unconfigured", "message": "AI service is not configured."},
    )
    system_text = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
    conversation_messages = messages[1 if system_text else 0:]
    prompt = _conversation_as_text(conversation_messages) or _conversation_as_text(messages)
    configured_url = settings.ai_api_url.strip()
    targets: list[tuple[str | None, str]] = (
        [(None, configured_url)]
        if configured_url
        else [
            (
                model,
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            )
            for model in _gemini_model_candidates()
        ]
    )
    last_error: Exception | None = None
    rate_limited_models: list[str] = []
    header_options = _gemini_auth_header_options(api_key)
    async with httpx.AsyncClient(timeout=settings.ai_request_timeout_seconds) as client:
        for model, url in targets:
            for headers in header_options:
                try:
                    contents: list[dict[str, Any]] = []
                    for message in conversation_messages:
                        role = "model" if message.get("role") == "assistant" else "user"
                        content = (message.get("content") or "").strip()
                        if content:
                            contents.append({"role": role, "parts": [{"text": content}]})
                    payload = {
                        "contents": contents or [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "maxOutputTokens": (
                                max(settings.ai_max_output_tokens, 1200)
                                if model and model.startswith("gemini-3")
                                else settings.ai_max_output_tokens
                            ),
                        },
                    }
                    if model and model.startswith("gemini-3"):
                        payload["generationConfig"]["thinkingConfig"] = {
                            "thinkingLevel": "low"
                        }
                    elif model and model.startswith("gemini-2.5"):
                        payload["generationConfig"]["thinkingConfig"] = {
                            "thinkingBudget": 0
                        }
                    if system_text.strip():
                        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
                    response = await client.post(
                        url,
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    answer = _extract_gemini_answer(response.json())
                    if answer:
                        return answer
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status_code = exc.response.status_code
                    if status_code in {401, 403}:
                        raise HTTPException(
                            status_code=503,
                            detail={
                                "code": "ai_provider_auth_failed",
                                "message": "AI service authentication failed.",
                            },
                        ) from exc
                    if status_code == 429:
                        if model:
                            rate_limited_models.append(model)
                        # Quotas can differ by model. Try every configured safe
                        # candidate before falling back to the local response.
                        break
                    continue
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    last_error = exc
                    continue
    if rate_limited_models:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ai_provider_rate_limited",
                "message": "AI service quota is temporarily unavailable.",
            },
        ) from last_error
    if isinstance(last_error, httpx.HTTPStatusError) and last_error.response.status_code == 404:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ai_model_unavailable",
                "message": "The configured AI model is unavailable.",
            },
        ) from last_error
    raise HTTPException(
        status_code=503,
        detail={"code": "ai_provider_failed", "message": "AI service is temporarily unavailable."},
    ) from last_error


async def _ai_answer(
    question: str,
    context: str,
    *,
    language: str = "ar",
    customer_name: str = "عزيزي العميل",
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    settings = get_settings()
    if not settings.resolved_ai_api_key or (_ai_provider_kind() != "gemini" and not settings.ai_api_url):
        raise HTTPException(
            status_code=503,
            detail={"code": "ai_provider_unconfigured", "message": "AI service is not configured."},
        )
    if language == "en":
        system_prompt = f"""You are Noura, a warm professional sales and customer service assistant for Luxury Shopping in Yemen.

Personality:
- Helpful, accurate, polite, confident, and conversational.
- Answer the customer's exact question first, then add the useful shopping step.
- Give enough detail to be useful, but do not pad or repeat.
- Do not repeat the same sentence.
- Use the customer name naturally when available: {customer_name}.
- Never invent order, payment, or stock facts that are not in the provided context.
- If the question needs live external information outside the provided context, say that you do not have a verified live source for that detail and explain the safe next step.
- If products, offers, categories, stores, or orders are present in the context, use only customer-safe names and guidance.
- If the context contains "Verified app guidance", follow that procedure exactly. Do not replace an app-how-to answer with product recommendations or an unrelated list.
- Only mention products when the customer asks for a product, offer, gift, price, category, or recommendation. Never add product names just to fill an answer.
- Never reveal internal catalog counts, database wording, coupon codes, raw IDs, hashes, staff/admin statuses, or technical record metadata.
- Product names and customer-visible prices in the context are public enough to recommend, but keep the answer to 2-4 concise lines.
- For offers or gift recommendations, name up to three options with customer-visible prices and one short reason or next step.
- Use only the official domain when sharing links: https://luxuryshoppings.com

Services:
- Premium products.
- International shopping.
- Local shopping.
- Offers, coupons, loyalty, shipping, returns, and customer support.

Context:
{context}"""
    else:
        system_prompt = f"""أنتِ نورة، مساعدة مبيعات وخدمة عملاء محترفة في متجر رفاهية التسوق في اليمن.

الشخصية والأسلوب:
- لبقة، دقيقة، دافئة، ومفيدة.
- تحدثي بالعربية الواضحة القريبة من العميل اليمني، بدون لهجة مصرية.
- أجيبي على سؤال العميل نفسه أولاً، ثم أضيفي الخطوة المناسبة داخل التسوق إذا كانت مفيدة.
- أعطي تفاصيل كافية ومباشرة، بدون تطويل ممل وبدون تكرار.
- أجيبي حتى على الأسئلة العامة، لكن إذا احتاج السؤال معلومة خارجية مباشرة غير موجودة في السياق فقولي بوضوح إنك لا تملكين مصدرًا مباشرًا مؤكدًا لها الآن، ثم قدّمي أفضل خطوة آمنة.
- لا تكرري نفس الجمل.
- استخدمي اسم العميل عند توفره بدون مبالغة: {customer_name}.
- لا تخترعي حالة طلب أو دفع أو مخزون غير موجودة في السياق.
 - إذا وجدت منتجات أو عروض أو تصنيفات أو متاجر في السياق، استخدمي أسماء آمنة للعميل فقط وباختصار.
 - إذا احتوى السياق على عبارة «إجابة إجرائية مؤكدة للتطبيق»، اتبعي الخطوات كما هي ولا تستبدليها بترشيحات منتجات أو قائمة لا علاقة لها بالسؤال.
 - لا تذكري أسماء منتجات إلا إذا سأل العميل عن منتج أو عرض أو هدية أو سعر أو تصنيف أو ترشيح. لا تضيفي منتجات لمجرد ملء الرد.
 - لا تذكري أرقام العد، أو عبارة قاعدة الموقع، أو أكواد الخصم، أو المعرفات، أو الحالات الداخلية، أو تفاصيل تشغيلية.
 - أسماء المنتجات والأسعار الظاهرة للعميل مسموح استخدامها للترشيح، لكن اجعلي الرد من 2 إلى 4 أسطر مختصرة.
 - عند سؤال العروض أو الهدايا، اذكري حتى ثلاثة خيارات مع السعر الظاهر وسبب قصير أو خطوة تالية.
 - لا تعرضي تفاصيل الطلبات الخاصة داخل الدردشة؛ وجّهي العميل إلى صفحة طلباتي.
- عند الروابط استخدمي النطاق الرسمي فقط: https://luxuryshoppings.com

الخدمات:
- منتجات فاخرة.
- شراء دولي.
- تسوق محلي.
- عروض وكوبونات وولاء وشحن وإرجاع ودعم.

السياق:
{context}"""
    system_prompt += """

Output contract:
- Return only the final customer-facing answer. Never output analysis, hidden reasoning, plans, checklists, self-evaluation, or labels such as first/final.
- Answer in the exact language used by the customer unless the customer explicitly asks for translation.
- Treat Arabic and English customer text as valid UTF-8. Never claim that a readable customer message is garbled, encoded incorrectly, or made of unclear symbols.
- Start with the direct answer, not a repeated greeting.
- Keep the final answer complete and concise. Never stop mid-sentence.
"""
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        for item in conversation_history[-10:]:
            if not isinstance(item, dict):
                continue
            role = "user" if item.get("role") == "user" else "assistant"
            content = str(item.get("content") or "").strip()
            if content:
                messages.append({"role": role, "content": content[:1000]})
    messages.append({"role": "user", "content": question})
    if _ai_provider_kind() == "gemini":
        return await _gemini_answer(messages)
    try:
        async with httpx.AsyncClient(timeout=settings.ai_request_timeout_seconds) as client:
            response = await client.post(
                settings.ai_api_url,
                headers={"Authorization": f"Bearer {settings.resolved_ai_api_key}"},
                json={
                    "model": settings.ai_default_model,
                    "messages": messages,
                    "max_tokens": settings.ai_max_output_tokens,
                    "temperature": 0.75,
                },
            )
            response.raise_for_status()
            payload = response.json()
            answer = payload.get("answer") or payload.get("output_text")
            if not answer and isinstance(payload.get("choices"), list) and payload["choices"]:
                answer = (payload["choices"][0].get("message") or {}).get("content")
            if not answer:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "ai_provider_failed", "message": "AI service is temporarily unavailable."},
                )
            return str(answer).strip()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(
            status_code=503,
            detail={"code": "ai_provider_failed", "message": "AI service is temporarily unavailable."},
        )


def _extract_chat_search_terms(message: str) -> list[str]:
    stopwords = {
        "ابحث", "بحث", "ابي", "أبي", "اريد", "أريد", "اشتي", "بغيت", "منتج", "منتجات",
        "عن", "في", "من", "على", "وش", "ايش", "إيش", "ارني", "اعرض", "عروض", "عرض",
        "هل", "كيف", "ممكن", "لو", "لو سمحت", "عندي", "فيه", "في", "هذا", "هذه", "ذلك",
        "هدية", "هديه", "افضل", "أفضل", "ميزانية", "حدود", "بحدود", "سعر", "اقل", "أقل", "تحت",
        "show", "find", "search", "product", "products", "for", "me", "please", "want",
        "gift", "present", "budget", "price", "recommend", "best", "under", "below",
    }
    cleaned = re.sub(r"[^\w\u0600-\u06FF\s-]", " ", message.lower())
    terms: list[str] = []
    for word in cleaned.split():
        if _normalize_chat_digits(word).isdigit():
            continue
        if len(word) < 3 or word in stopwords:
            continue
        if word not in terms:
            terms.append(word)
    return terms[:5]


def _plain_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _display_text(*values: Any, default: str = "") -> str:
    for value in values:
        text = _plain_text(value)
        if text:
            return text
    return default


def _resource_active_clauses(model: type[Any]) -> list[Any]:
    table = model.__table__
    clauses: list[Any] = []
    if "deleted_at" in table.c:
        clauses.append(table.c.deleted_at.is_(None))
    if "is_active" in table.c:
        clauses.append(table.c.is_active.is_(True))
    if "status" in table.c:
        normalized = func.lower(func.trim(func.coalesce(table.c.status, "")))
        clauses.append(
            normalized.notin_(
                [
                    "inactive",
                    "disabled",
                    "deleted",
                    "archived",
                    "rejected",
                    "suspended",
                    "blocked",
                    "draft",
                    "hidden",
                ]
            )
        )
    return clauses


def _order_by_recent(model: type[Any]) -> Any:
    table = model.__table__
    if "updated_at" in table.c:
        return table.c.updated_at.desc()
    if "created_at" in table.c:
        return table.c.created_at.desc()
    return table.c.id.desc()


def _money_text(value: Any, language: str) -> str:
    amount = str(money(Decimal(str(value or 0))))
    return f"{amount} YER" if language == "en" else f"{amount} ر.ي"


_CHAT_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _normalize_chat_digits(value: str) -> str:
    return str(value or "").translate(_CHAT_DIGIT_TRANSLATION)


def _extract_chat_budget(message: str) -> Decimal | None:
    normalized = _normalize_chat_digits(message)
    if not _has_any_chat_term(
        normalized,
        ["budget", "price", "gift", "present", "under", "below", "حدود", "ميزانية", "سعر", "هدية", "هديه", "اقل", "أقل", "تحت"],
    ):
        return None
    thousands_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:ألف|الف|آلاف)",
        normalized,
        re.IGNORECASE,
    )
    if thousands_match:
        return Decimal(thousands_match.group(1)) * Decimal("1000")

    arabic_thousands = {
        "عشرة": 10,
        "عشرون": 20,
        "عشرين": 20,
        "ثلاثون": 30,
        "ثلاثين": 30,
        "أربعون": 40,
        "اربعون": 40,
        "أربعين": 40,
        "اربعين": 40,
        "خمسون": 50,
        "خمسين": 50,
        "ستون": 60,
        "ستين": 60,
        "سبعون": 70,
        "سبعين": 70,
        "ثمانون": 80,
        "ثمانين": 80,
        "تسعون": 90,
        "تسعين": 90,
        "مئة": 100,
        "مائة": 100,
    }
    for word, amount in arabic_thousands.items():
        if re.search(rf"\b{word}\s+(?:ألف|الف|آلاف)\b", normalized):
            return Decimal(amount) * Decimal("1000")

    candidates: list[Decimal] = []
    for raw in re.findall(r"\d[\d,\s]*(?:\.\d+)?", normalized):
        cleaned = re.sub(r"[,\s]", "", raw)
        try:
            amount = Decimal(cleaned)
        except Exception:
            continue
        if amount >= Decimal("100"):
            candidates.append(amount)
    return max(candidates) if candidates else None


def _safe_product_summary(product: Any, language: str) -> str:
    name = _safe_customer_name(
        getattr(product, "promotional_title", None),
        getattr(product, "name", None),
        getattr(product, "name_en", None),
        fallback="",
    )
    if not name:
        return ""
    current_price = _money_text(getattr(product, "price", None), language)
    original_price = getattr(product, "original_price", None)
    try:
        has_discount = original_price is not None and Decimal(str(original_price)) > Decimal(str(getattr(product, "price", 0) or 0))
    except Exception:
        has_discount = False
    if has_discount:
        old_price = _money_text(original_price, language)
        return f"{name} — {current_price} instead of {old_price}" if language == "en" else f"{name} — {current_price} بدل {old_price}"
    return f"{name} — {current_price}"


def _join_safe_summaries(items: list[Any], language: str, *, limit: int = 3) -> str:
    summaries = [_safe_product_summary(item, language) for item in items]
    summaries = [summary for summary in summaries if summary]
    if not summaries:
        return ""
    separator = "; " if language == "en" else "؛ "
    return separator.join(summaries[:limit])


_CUSTOMER_SAFE_INTERNAL_TEXT_RE = re.compile(
    r"(database(?:\s+(?:url|host|name|record|table))?|postgres(?:ql)?|uuid|"
    r"public catalog|matching products|discounted products|jwt(?:\s+secret)?|"
    r"access[_\s-]?token|refresh[_\s-]?token|api[_\s-]?key|"
    r"category\s+[0-9a-f]{6,}|store\s+[0-9a-f]{6,}|"
    r"\b[A-Z]{3,}[A-Z0-9]{7,}\b|[0-9a-f]{12,}|"
    r"قاعدة\s+(?:البيانات|الموقع)|رابط\s+قاعدة|مفتاح\s+(?:سري|واجهة|api)|"
    r"رمز\s+(?:وصول|تحديث)|عدد\s+المنتجات\s+(?:المنشورة|المطابقة|المخفضة)|"
    r"(?:بيانات|سجل|متجر|منتج)\s+اختبار)",
    re.IGNORECASE,
)

_PUBLIC_CHAT_LEAK_RE = re.compile(
    r"(هذه\s+أقرب\s+معلومات\s+مؤكدة|قاعدة\s+الموقع|عدد\s+المنتجات|"
    r"عدد\s+.*المطابقة|عدد\s+.*المخفضة|كوبونات\s+فعالة|active\s+coupons|"
    r"\bSAV[A-Z0-9]{4,}\b|test\s+coupon|category\s+[0-9a-f]{6,}|store\s+[0-9a-f]{6,}|"
    r"\((?:active|inactive|published|approved|pending)\)|postgresql|database)",
    re.IGNORECASE,
)

_PUBLIC_CHAT_INSTRUCTION_RE = re.compile(
    r"(هذا\s+سؤال\s+عام\s+لا\s+يحتاج\s+بيانات\s+المتجر|"
    r"أجب\s+عنه\s+مباشرة\s+من\s+معرفتك|"
    r"لا\s+تشارك\s+أي\s+أسرار\s+أو\s+روابط\s+قواعد\s+بيانات|"
    r"this\s+is\s+a\s+general\s+question\s+that\s+does\s+not\s+need\s+store\s+data|"
    r"do\s+not\s+share\s+secrets,?\s+database\s+links)",
    re.IGNORECASE,
)


def _is_public_chat_instruction(value: str) -> bool:
    return bool(_PUBLIC_CHAT_INSTRUCTION_RE.search(value or ""))


def _safe_public_chat_replacement(language: str) -> str:
    return (
        "I can help with current offers, products, stores, orders, payment, shipping, and returns. Tell me the category, budget, or product you want and I will guide you clearly."
        if language == "en"
        else "أكيد، أقدر أساعدك في العروض والمنتجات والمتاجر والطلبات والدفع والشحن والإرجاع. اكتب القسم أو الميزانية أو اسم المنتج، وبأرشدك للخيار المناسب باختصار."
    )


def _looks_like_unusable_ai_answer(value: str, language: str) -> bool:
    text = _normalize_chat_text(value)
    if not text or text == _normalize_chat_text(_safe_public_chat_replacement(language)):
        return True
    if _is_public_chat_instruction(value):
        return True
    return _has_any_chat_term(
        text,
        [
            "رسالتك ظهرت برموز",
            "نص رسالتك ظهر بلغة غير مفهومة",
            "نص رسالتك لم يظهر بشكل واضح",
            "رموز غير واضحة",
            "garbled message",
            "unreadable message",
            "unclear symbols",
        ],
    )


def _answer_matches_chat_intent(message: str, answer: str) -> bool:
    direct_guidance = _chat_direct_guidance(message, "ar") or _chat_direct_guidance(message, "en")
    if direct_guidance:
        normalized_message = _normalize_chat_text(message)
        if _has_any_chat_term(
            message,
            ["سلة", "السلة", "عربة", "عربه", "cart"],
        ) and _has_any_chat_term(
            answer,
            ["سلة", "السلة", "عربة", "عربه", "cart", "add", "اضافة", "إضافة", "حذف", "إزالة", "remove", "delete"],
        ):
            return True
        if _has_any_chat_term(
            message,
            ["مفضلة", "المفضلة", "امنيات", "أمنيات", "wishlist", "favorites"],
        ) and _has_any_chat_term(
            answer,
            ["مفضلة", "المفضلة", "امنيات", "أمنيات", "wishlist", "favorites", "قلب", "heart", "remove", "إزالة"],
        ):
            return True
        if _has_any_chat_term(
            message,
            ["واقع معزز", "الواقع المعزز", "تجربة المنتج", "try-on", "try on", "augmented reality"],
        ) or bool(re.search(r"\bar\b", normalized_message)):
            return _has_any_chat_term(
                answer,
                ["واقع معزز", "الواقع المعزز", "تجربة", "الكاميرا", "ar", "try-on", "try on", "camera"],
            )
        if _has_any_chat_term(message, ["تتبع", "طلباتي", "رقم الطلب", "tracking", "my orders"]):
            return _has_any_chat_term(answer, ["تتبع", "طلباتي", "حالة", "tracking", "my orders", "order"])
        if _has_any_chat_term(message, ["شراء", "اشتري", "أشتري", "اكمل الطلب", "إتمام الطلب", "checkout", "buy"]):
            return _has_any_chat_term(answer, ["شراء", "السلة", "الطلب", "تسجيل دخول", "checkout", "cart", "sign in"])
        if _has_any_chat_term(message, ["مقارنة", "قارن", "مقارنه", "compare"]):
            return _has_any_chat_term(answer, ["مقارنة", "قارن", "compare"])
        if _has_any_chat_term(message, ["مشاركة", "شارك", "مشاركه", "share"]):
            return _has_any_chat_term(answer, ["مشاركة", "شارك", "رابط", "share", "link"])
        if _has_any_chat_term(message, ["دفع", "تحويل", "payment", "receipt"]):
            return _has_any_chat_term(answer, ["دفع", "تحويل", "إيصال", "payment", "receipt", "checkout"])
        if _has_any_chat_term(message, ["شحن", "توصيل", "shipping", "delivery"]):
            return _has_any_chat_term(answer, ["شحن", "توصيل", "shipping", "delivery", "checkout"])
        if _has_any_chat_term(message, ["ارجاع", "إرجاع", "استبدال", "return", "refund", "exchange"]):
            return _has_any_chat_term(answer, ["ارجاع", "إرجاع", "استبدال", "return", "refund", "exchange", "دعم", "support"])
    category_terms = ["تصنيف", "تصنيفات", "قسم", "اقسام", "category", "categories"]
    if _has_any_whole_chat_term(message, category_terms):
        return _has_any_chat_term(answer, category_terms)
    checks = (
        (["عرض", "عروض", "خصم", "offer", "discount"], ["عرض", "عروض", "خصم", "مخفض", "offer", "discount", "ر.ي", "ريال"]),
        (["شحن", "توصيل", "shipping", "delivery"], ["شحن", "توصيل", "shipping", "delivery"]),
        (["هدية", "هديه", "gift", "present"], ["هدية", "هديه", "اختيار", "خيار", "gift", "present", "ر.ي", "ريال"]),
        (["ايش يسوي الموقع", "خدمات الموقع", "site services", "what does the site do"], ["رفاهية التسوق", "منتجات", "طلبات", "shopping", "products", "orders"]),
    )
    for question_terms, answer_terms in checks:
        if _has_any_chat_term(message, question_terms):
            return _has_any_chat_term(answer, answer_terms)
    return True


def _looks_like_specific_customer_answer(value: str, language: str) -> bool:
    text = _normalize_chat_text(value)
    fallback = _normalize_chat_text(_safe_public_chat_replacement(language))
    if not text or text == fallback:
        return False
    return _has_any_chat_term(
        value,
        [
            "عروض",
            "خصم",
            "هدايا",
            "هدية",
            "هديه",
            "خيارات",
            "تفاصيل الشحن",
            "طرق الدفع",
            "رفاهية التسوق",
            "المتاجر",
            "التصنيفات",
            "منتج",
            "products",
            "offers",
            "gift",
            "shipping",
            "payment",
            "stores",
            "categories",
        ],
    )


def _safe_customer_name(*values: Any, fallback: str = "") -> str:
    text = _display_text(*values, default=fallback)
    if not text:
        return fallback
    if _CUSTOMER_SAFE_INTERNAL_TEXT_RE.search(text):
        return fallback
    return text[:80]


def _join_customer_names(values: list[str], *, language: str, limit: int = 3) -> str:
    cleaned = [value for value in values if value]
    if not cleaned:
        return ""
    separator = ", " if language == "en" else "، "
    return separator.join(cleaned[:limit])


def _customer_safe_site_context(
    *,
    message: str,
    language: str,
    products: list[Product],
    offers: list[Product],
    categories: list[Category],
    stores: list[Any],
    orders: list[Order],
    user: User | None,
    wants_offers: bool,
    wants_categories: bool,
    wants_stores: bool,
    wants_orders: bool,
    terms: list[str],
) -> tuple[str, bool]:
    english = language == "en"
    product_names = [_safe_customer_name(item.name, item.name_en) for item in products]
    offer_names = [_safe_customer_name(item.promotional_title, item.name, item.name_en) for item in offers]
    category_names = [_safe_customer_name(category.name, category.name_en) for category in categories]
    store_names = [
        _safe_customer_name(getattr(store, "name", None), getattr(store, "name_en", None))
        for store in stores
    ]

    lines: list[str] = []
    if wants_offers:
        joined = _join_customer_names(offer_names, language=language)
        lines.append(
            f"There are selected offers now{f' such as: {joined}' if joined else ''}. Open the Offers page for current details."
            if english
            else f"في عروض مختارة الآن{f' مثل: {joined}' if joined else ''}. افتح صفحة العروض للتفاصيل الحالية."
        )
        lines.append(
            "If you have a discount code shown to you, enter it during checkout."
            if english
            else "إذا ظهر لك رمز خصم داخل الموقع، استخدمه عند إتمام الطلب."
        )
    elif products:
        joined = _join_customer_names(product_names, language=language)
        lines.append(
            f"I found suitable options such as: {joined}. Open Products and filter by category, brand, or price."
            if english
            else f"لقيت لك خيارات مناسبة مثل: {joined}. افتح المنتجات وفلتر حسب القسم أو الماركة أو السعر."
        )
    elif terms:
        lines.append(
            "I did not find a direct match for those exact words. Try a product name, category, or brand."
            if english
            else "ما لقيت نتيجة مباشرة بنفس الكلمات. جرّب اسم المنتج أو القسم أو الماركة."
        )
    else:
        lines.append(
            "I can help with products, offers, stores, orders, payment, shipping, and returns."
            if english
            else "أقدر أساعدك في المنتجات والعروض والمتاجر والطلبات والدفع والشحن والإرجاع."
        )

    if wants_categories:
        joined = _join_customer_names(category_names, language=language, limit=5)
        if joined:
            lines.append(
                f"Available sections include: {joined}."
                if english
                else f"من الأقسام المتاحة: {joined}."
            )

    if wants_stores:
        joined = _join_customer_names(store_names, language=language, limit=5)
        if joined:
            lines.append(
                f"You can browse stores such as: {joined}."
                if english
                else f"تقدر تتصفح متاجر مثل: {joined}."
            )

    if wants_orders:
        if user is None:
            lines.append(
                "Sign in first, then open My Orders for private tracking updates."
                if english
                else "سجّل دخولك أولاً، ثم افتح طلباتي لمتابعة طلباتك الخاصة بأمان."
            )
        elif orders:
            lines.append(
                "I found recent orders in your account. Open My Orders to view the private details safely."
                if english
                else "لقيت طلبات حديثة في حسابك. افتح طلباتي لعرض التفاصيل الخاصة بأمان."
            )
        else:
            lines.append(
                "I did not find recent orders in your account."
                if english
                else "ما ظهر لي طلب حديث في حسابك حالياً."
            )

    return (_sanitize_public_chat_response("\n".join(lines), language), bool(products))


def _customer_safe_site_context_v2(
    *,
    message: str,
    language: str,
    products: list[Any],
    offers: list[Any],
    categories: list[Any],
    stores: list[Any],
    orders: list[Order],
    user: User | None,
    wants_offers: bool,
    wants_categories: bool,
    wants_stores: bool,
    wants_orders: bool,
    wants_shipping: bool,
    wants_payment: bool,
    wants_site_info: bool,
    wants_gift: bool,
    wants_featured: bool,
    budget: Decimal | None,
    terms: list[str],
) -> tuple[str, bool]:
    english = language == "en"
    product_summaries = _join_safe_summaries(products, language, limit=3)
    offer_summaries = _join_safe_summaries(offers, language, limit=3)
    category_names = [
        _safe_customer_name(getattr(category, "name", None), getattr(category, "name_en", None))
        for category in categories
    ]
    store_names = [
        _safe_customer_name(getattr(store, "name", None), getattr(store, "name_en", None))
        for store in stores
    ]

    lines: list[str] = []
    if wants_site_info:
        lines.append(
            "Luxury Shopping helps you browse products, offers, stores, cart, checkout, payment, delivery tracking, and returns from one place."
            if english
            else "رفاهية التسوق منصة تجمع لك المنتجات والعروض والمتاجر في مكان واحد. تقدر تبحث، تقارن، تضيف للسلة، تكمل الطلب، وتتابع الشحن من حسابك."
        )
    elif wants_shipping:
        lines.append(
            "Shipping details appear at checkout after choosing the address and delivery option. After ordering, track updates from My Orders."
            if english
            else "تفاصيل الشحن تظهر أثناء إتمام الطلب بعد اختيار العنوان وطريقة التوصيل. بعد إنشاء الطلب تقدر تتابع الحالة والتحديثات من طلباتي."
        )
    elif wants_payment:
        lines.append(
            "Payment options appear during checkout based on the enabled methods. For transfer payments, upload the receipt from the order page."
            if english
            else "طرق الدفع تظهر أثناء إتمام الطلب حسب المتاح. إذا اخترت التحويل، ارفع الإيصال من صفحة الطلب حتى تتم مراجعته."
        )
    elif wants_categories and not wants_offers and not wants_gift:
        joined = _join_customer_names(category_names, language=language, limit=6)
        lines.append(
            f"You can browse sections like: {joined}. Choose a section and I will narrow products for you."
            if english and joined
            else (
                "Tell me the style or product you want and I will suggest the closest section."
                if english
                else f"تقدر تتصفح أقسام مثل: {joined}. اختر القسم وأنا أضيق لك المنتجات المناسبة."
                if joined
                else "اكتب نوع المنتج أو الستايل الذي تريده، وبأقترح لك أقرب قسم."
            )
        )
    elif wants_offers:
        if offer_summaries:
            lines.append(
                f"My closest offer picks: {offer_summaries}. Open Offers to confirm availability before checkout."
                if english
                else f"أقرب عروض مناسبة الآن: {offer_summaries}. افتح صفحة العروض لتأكيد التوفر قبل الطلب."
            )
        else:
            lines.append(
                "Open Offers for the current discounted items, or tell me a category or budget."
                if english
                else "توجد عروض مختارة تتغير حسب التوفر. افتح صفحة العروض، أو اكتب القسم أو الميزانية وبأرشح لك خيارًا مناسبًا."
            )
    elif wants_featured:
        if product_summaries:
            lines.append(
                f"Featured products: {product_summaries}. Open Products to see images, options, and final availability."
                if english
                else f"المنتجات المميزة الآن: {product_summaries}. افتح المنتجات لمشاهدة الصور والخيارات والتوفر النهائي."
            )
        else:
            lines.append(
                "Open Featured Products to see the store's highlighted picks."
                if english
                else "افتح قسم المنتجات المميزة لمشاهدة اختيارات المتجر المميزة."
            )
    elif wants_gift or budget is not None:
        budget_text = _money_text(budget, language) if budget is not None else ""
        recommendation_summaries = product_summaries or offer_summaries
        if recommendation_summaries:
            lines.append(
                f"Good gift options{f' around {budget_text}' if budget_text else ''}: {recommendation_summaries}. Choose the one that matches the recipient style."
                if english
                else f"أنسب هدايا{f' بحدود {budget_text}' if budget_text else ''}: {recommendation_summaries}. اختَر حسب ذوق الشخص ونوع المناسبة."
            )
        else:
            lines.append(
                f"I did not find a clear option within {budget_text}. Try a slightly higher budget or tell me the gift type."
                if english and budget_text
                else (
                    "Tell me the gift type and budget and I will suggest suitable choices."
                    if english
                    else f"ما ظهر لي خيار واضح ضمن {budget_text}. جرّب ميزانية أعلى قليلًا أو اكتب نوع الهدية."
                    if budget_text
                    else "اكتب نوع الهدية والميزانية، وبأقترح لك خيارات مناسبة."
                )
            )
    elif product_summaries:
        lines.append(
            f"Suitable options: {product_summaries}. Open Products to see images, sizes, and final availability."
            if english
            else f"خيارات مناسبة: {product_summaries}. افتح المنتجات لمشاهدة الصور والمقاسات والتوفر النهائي."
        )
    elif terms:
        lines.append(
            "I did not find an exact match. Try a simpler product name, category, brand, or budget."
            if english
            else "ما لقيت نتيجة بنفس الكلمات. جرّب اسم أبسط للمنتج أو القسم أو الماركة أو الميزانية."
        )
    else:
        lines.append(
            "Ask me about products, offers, stores, orders, payment, shipping, returns, or a general question."
            if english
            else "اسألني عن المنتجات، العروض، المتاجر، الطلبات، الدفع، الشحن، الإرجاع، أو أي سؤال عام."
        )

    if wants_stores:
        joined = _join_customer_names(store_names, language=language, limit=4)
        if joined:
            lines.append(
                f"Stores you can browse include: {joined}."
                if english
                else f"من المتاجر التي تقدر تتصفحها: {joined}."
            )

    if wants_orders:
        if user is None:
            lines.append(
                "Sign in, then open My Orders to track private updates safely."
                if english
                else "سجّل دخولك ثم افتح طلباتي لمتابعة التحديثات الخاصة بأمان."
            )
        elif orders:
            lines.append(
                "Open My Orders to view your private order details safely."
                if english
                else "افتح طلباتي لعرض تفاصيل طلبك الخاصة بأمان."
            )
        else:
            lines.append(
                "I do not see a recent order in your account right now."
                if english
                else "ما يظهر لي طلب حديث في حسابك حالياً."
            )

    return (_sanitize_public_chat_response("\n".join(lines), language), bool(products or offers))


def _sanitize_public_chat_response(value: str, language: str) -> str:
    raw_value = str(value or "")
    cleaned_lines: list[str] = []
    for raw_line in raw_value.splitlines():
        line = raw_line.strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1]:
                cleaned_lines.append("")
            continue
        if re.search(
            r"^(?:كوبونات\s+فعالة|Active\s+coupons)\s*:",
            line,
            re.IGNORECASE,
        ):
            continue
        if _PUBLIC_CHAT_LEAK_RE.search(line):
            continue
        if _PUBLIC_CHAT_INSTRUCTION_RE.search(line):
            continue
        if _CUSTOMER_SAFE_INTERNAL_TEXT_RE.search(line):
            continue
        if re.search(r"(عدد\s+المنتجات|عدد\s+.*المطابقة|عدد\s+.*المخفضة|count\s*:|products?\s+count)", line, re.IGNORECASE):
            continue
        if re.search(r"(كوبونات\s+فعالة|Coupons\s*:|coupon\s+code|SAV[A-Z0-9]{4,}|[A-Z]{3,}[A-Z0-9]{5,}\s*:)", line, re.IGNORECASE):
            continue
        has_customer_visible_price = bool(re.search(r"(ر\.ي|YER|SAR|ريال)", line, re.IGNORECASE))
        if re.search(r"^\s*-\s+", line) and not has_customer_visible_price and not re.search(r"(افتح|اختر|اسأل|Tell|Open|Choose|Ask)", line, re.IGNORECASE):
            continue
        if re.search(r"\b\d+\s*(active|published|matching|discounted|products?)\b", line, re.IGNORECASE):
            continue
        line = re.sub(r"\s*\((?:active|inactive|published|approved|pending)\)\s*", " ", line, flags=re.IGNORECASE).strip()
        cleaned_lines.append(line)
    response = "\n".join(cleaned_lines).strip()
    if response:
        return response[:900].strip()
    return _safe_public_chat_replacement(language)


async def _chat_site_context(
    session: AsyncSession,
    message: str,
    *,
    language: str,
    user: User | None,
) -> tuple[str, bool]:
    terms = _extract_chat_search_terms(message)
    wants_offers = _has_any_chat_term(message, ["عرض", "عروض", "خصم", "كوبون", "offer", "discount", "coupon"])
    wants_categories = _has_any_whole_chat_term(
        message,
        ["تصنيف", "تصنيفات", "قسم", "اقسام", "category", "categories"],
    )
    wants_stores = _has_any_chat_term(message, ["متجر", "متاجر", "تاجر", "partner", "merchant", "store", "stores"])
    wants_orders = _has_any_chat_term(message, ["طلب", "تتبع", "رقم الطلب", "order", "tracking"])
    wants_shipping = _has_any_chat_term(
        message,
        [
            "الشحن",
            "شحن",
            "تفاصيل الشحن",
            "معلومات الشحن",
            "طريقة الشحن",
            "تكلفة الشحن",
            "مدة الشحن",
            "التوصيل",
            "shipping",
            "shipping details",
            "delivery details",
            "shipping cost",
            "delivery option",
        ],
    )
    wants_payment = _has_any_chat_term(message, ["طرق الدفع", "طريقة الدفع", "دفع", "تحويل", "ايصال", "فاتوره", "payment", "receipt", "invoice"])
    wants_site_info = _has_any_chat_term(
        message,
        [
            "ايش يسوي الموقع",
            "وش يسوي الموقع",
            "ما هو الموقع",
            "عن الموقع",
            "خدمات الموقع",
            "خدمات",
            "يقدمها الموقع",
            "what does the site do",
            "what is this site",
            "what services",
            "site services",
        ],
    )
    wants_gift = _has_any_chat_term(message, ["هدية", "هديه", "اقتراح", "انصح", "افضل", "أفضل", "gift", "present", "recommend", "best"])
    wants_featured = _has_any_chat_term(
        message,
        ["مميز", "مميزه", "الافضل", "الأفضل", "الاكثر طلبا", "featured", "best product", "best products"],
    )
    budget = _extract_chat_budget(message)
    wants_internal_data = _has_any_chat_term(
        message,
        [
            "قاعدة البيانات",
            "رابط قاعدة",
            "مفتاح سري",
            "كلمة مرور",
            "jwt",
            "secret",
            "token",
            "database url",
            "api key",
            "password",
        ],
    )
    catalog_intent = any(
        (
            wants_offers,
            wants_categories,
            wants_stores,
            wants_orders,
            wants_shipping,
            wants_payment,
            wants_site_info,
            wants_gift,
            wants_featured,
            budget is not None,
            _has_any_chat_term(
                message,
                [
                    "منتج", "منتجات", "ابحث", "سعر", "فستان", "حقيبة", "حقيبه",
                    "حذاء", "عطر", "مقاس", "ماركة", "السلة", "إرجاع", "استبدال",
                    "product", "search", "price", "dress", "bag", "shoe", "perfume",
                    "size", "brand", "cart", "return", "refund",
                ],
            ),
        )
    )
    if wants_internal_data:
        return (
            "لا تشارك أي أسرار أو روابط قواعد بيانات أو مفاتيح أو معرفات أو إحصاءات داخلية. ارفض الطلب بلطف ووجّه العميل إلى الخدمات العامة الآمنة."
            if language != "en"
            else "Do not share secrets, database links, keys, identifiers, or internal statistics. Refuse politely and offer safe public help.",
            False,
        )
    direct_guidance = _chat_direct_guidance(message, language)
    if direct_guidance:
        return (
            f"إجابة إجرائية مؤكدة للتطبيق: {direct_guidance}"
            if language != "en"
            else f"Verified app guidance: {direct_guidance}",
            False,
        )
    if not catalog_intent:
        return (
            "هذا سؤال عام لا يحتاج بيانات المتجر. أجب عنه مباشرة من معرفتك العامة وبنفس لغة العميل، من دون تحويله إلى إعلان أو قائمة منتجات."
            if language != "en"
            else "This is a general question that does not need store data. Answer it directly from general knowledge in the customer's language without turning it into a product promotion.",
            False,
        )
    if wants_gift or wants_featured or budget is not None:
        terms = []

    public_product_filters = [Product.is_active.is_(True), *public_product_clauses(Product)]

    product_filters = list(public_product_filters)
    if budget is not None:
        product_filters.append(Product.price <= budget)
    if terms:
        term_filters = []
        for term in terms:
            pattern = f"%{term}%"
            term_filters.extend(
                [
                    Product.name.ilike(pattern),
                    Product.name_en.ilike(pattern),
                    Product.description.ilike(pattern),
                    Product.promotional_title.ilike(pattern),
                    Product.sku.ilike(pattern),
                ]
            )
        product_filters.append(or_(*term_filters))

    statement = (
        select(Product)
        .where(*product_filters)
        .order_by(Product.is_featured.desc(), Product.created_at.desc())
        .limit(8)
    )
    products = list((await session.execute(statement)).scalars())

    offer_filters = [
        *public_product_filters,
        Product.original_price.is_not(None),
        Product.original_price > Product.price,
    ]
    if budget is not None:
        offer_filters.append(Product.price <= budget)
    offers = []
    if wants_offers or wants_gift or budget is not None or not terms:
        offers = list(
            (
                await session.execute(
                    select(Product)
                    .where(*offer_filters)
                    .order_by(Product.is_featured.desc(), Product.updated_at.desc())
                    .limit(5)
                )
            ).scalars()
        )

    categories = list(
        (
            await session.execute(
                select(Category)
                .where(Category.deleted_at.is_(None), Category.is_active.is_(True))
                .order_by(Category.sort_order.asc(), Category.name.asc())
                .limit(10 if wants_categories or not terms else 5)
            )
        ).scalars()
    )

    stores: list[Any] = []
    for table_name in ("partner_storefronts", "local_merchants"):
        model = MODEL_BY_TABLE.get(table_name)
        if model is None:
            continue
        store_statement = (
            select(model)
            .where(*_resource_active_clauses(model))
            .order_by(_order_by_recent(model))
            .limit(5)
        )
        stores.extend(list((await session.execute(store_statement)).scalars()))

    orders: list[Order] = []
    if user is not None and wants_orders:
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.user_id == user.id, Order.deleted_at.is_(None))
                    .order_by(Order.created_at.desc())
                    .limit(5)
                )
            ).scalars()
        )

    return _customer_safe_site_context_v2(
        message=message,
        language=language,
        products=products,
        offers=offers,
        categories=categories,
        stores=stores,
        orders=orders,
        user=user,
        wants_offers=wants_offers,
        wants_categories=wants_categories,
        wants_stores=wants_stores,
        wants_orders=wants_orders,
        wants_shipping=wants_shipping,
        wants_payment=wants_payment,
        wants_site_info=wants_site_info,
        wants_gift=wants_gift,
        wants_featured=wants_featured,
        budget=budget,
        terms=terms,
    )


async def _chat_customer_name(session: AsyncSession, user: User | None) -> str:
    # The public assistant must never read or forward customer identity data.
    return "عزيزي العميل"


async def execute_public_ai_chat(
    body: dict[str, Any],
    user: User | None,
    session: AsyncSession,
    request: Request,
) -> dict[str, Any]:
    message = _text(body, "message", "question")
    if not message:
        raise HTTPException(status_code=400, detail="message_required")
    language = "en" if body.get("language") == "en" else "ar"
    site_context, has_products = await _chat_site_context(
        session,
        message,
        language=language,
        user=None,
    )
    customer_name = "عزيزي العميل"
    # Keep the provider request independent from customer-entered history.
    # Noura may use the current question and public catalog context only;
    # private messages, order identifiers, and contact details never enter
    # the model context.
    conversation_history = None
    action = _text(body, "action")
    context = "\n".join(
        part
        for part in (
            f"الإجراء المطلوب: {action}" if action else "",
            site_context,
            "إذا لم تتوفر معلومة مؤكدة، أجب بوضوح واطلب من العميل اختيار المنتج أو إرسال رقم الطلب.",
        )
        if part
    )
    try:
        raw_response = await _ai_answer(
            message,
            context,
            language=language,
            customer_name=customer_name,
            conversation_history=conversation_history,
        )
        response = _sanitize_public_chat_response(raw_response, language)
        if (
            _looks_like_unusable_ai_answer(response, language)
            or not _answer_matches_chat_intent(message, response)
        ):
            response = _fallback_chat_answer(message, language, site_context)
        configured = True
        provider_status = "ok"
    except HTTPException as exc:
        response = _fallback_chat_answer(message, language, site_context)
        configured = False
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        provider_status = str(detail.get("code") or "ai_provider_failed")
    response = _sanitize_public_chat_response(response, language)
    should_transfer = _has_any_chat_term(message, ["موظف", "انسان", "إنسان", "دعم مباشر", "واتساب", "agent", "human", "whatsapp"])
    return {
        "response": response,
        "transferToAgent": should_transfer,
        "hasProducts": has_products,
        "configured": configured,
        "providerStatus": provider_status,
        "request_id": current_request_id(),
    }


async def _reserve_ai_usage(
    *,
    function_name: str,
    body: dict[str, Any],
    user: User | None,
    roles: set[str],
    session: AsyncSession,
    request: Request,
) -> uuid.UUID:
    actor = _require_user(user)
    if body.get("provider") or body.get("ai_provider"):
        raise HTTPException(status_code=403, detail={"code": "ai_provider_selection_denied", "message": "Permission denied."})
    feature = "generation" if function_name in AI_GENERATION_FUNCTIONS else "recommendation"
    return await AIQuotaService(session).reserve(
        request=request,
        user_id=actor.id,
        roles=roles,
        feature=feature,
        model=str(body.get("model") or get_settings().ai_default_model),
        payload=body,
        idempotency_key=str(body.get("idempotencyKey") or body.get("idempotency_key") or request.headers.get("Idempotency-Key") or "").strip() or None,
    )


async def _approve_partner(
    session: AsyncSession,
    body: dict[str, Any],
    actor: User,
) -> dict[str, Any]:
    application_id = _uuid(
        body.get("application_id") or body.get("applicationId"),
        "application_id",
    )
    model = MODEL_BY_TABLE["partner_applications"]
    application = (
        await session.execute(select(model).where(model.id == application_id).with_for_update())
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail="application_not_found")
    if application.user_id is None:
        raise HTTPException(status_code=409, detail="application_user_missing")
    application.status = "approved"
    application.reviewed_by = actor.id
    application.reviewed_at = datetime.now(timezone.utc)
    approved_user = await session.get(User, application.user_id, with_for_update=True)
    if approved_user is not None:
        account_state = await account_security_for(session, approved_user.id, for_update=True)
        approved_user.is_active = True
        account_state.account_status = "active"
        account_state.disabled_at = None
        if account_state.email_verified_at is None:
            account_state.email_verified_at = application.reviewed_at
        await bump_security_version(session, approved_user, reason="merchant_approved")
    role = await session.get(UserRole, {"user_id": application.user_id, "role": "partner"})
    if role is None:
        session.add(UserRole(user_id=application.user_id, role="partner"))
    storefront_model = MODEL_BY_TABLE["partner_storefronts"]
    storefront = (
        await session.execute(
            select(storefront_model)
            .where(
                or_(
                    storefront_model.user_id == application.user_id,
                    storefront_model.partner_id == application.user_id,
                )
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if storefront is None:
        storefront = storefront_model(
            user_id=application.user_id,
            partner_id=application.user_id,
            name=application.name or "متجر",
            email=application.email,
            phone=application.phone,
            status="active",
            description=application.description,
            logo_url=application.logo_url,
            is_active=True,
        )
        session.add(storefront)
    else:
        storefront.status = "active"
        storefront.is_active = True
    await NotificationService(session).create_notification(
        NotificationPayload(
            user_id=application.user_id,
            title="تمت الموافقة على طلب متجرك",
            body="تمت الموافقة على طلب متجرك ويمكنك الآن تجهيز المنتجات للمراجعة.",
            notification_type="partner_application_approved",
            category="partner",
            priority="high",
            entity_type="partner_applications",
            entity_id=str(application.id),
            created_by=actor.id,
            deduplication_key=f"partner-application-review:{application.id}:approved",
        )
    )
    return {"ok": True, "application": serialize_record(application)}


async def execute_function(
    function_name: str,
    body: dict[str, Any],
    user: User | None,
    session: AsyncSession,
    request: Request,
) -> Any:
    roles = set(await roles_for(session, user.id)) if user else set()

    if function_name == "has_role":
        _require_user(user)
        requested = _text(body, "role", "_role", "p_role")
        return requested in roles
    if function_name == "is_staff":
        _require_user(user)
        return bool(roles.intersection(STAFF_ROLES))
    if function_name in RATE_LIMIT_ENUMERATION_FUNCTIONS:
        _require_user(user)
        _require_roles(roles, ADMIN_ROLES)
    if function_name in {"check_login_rate_limit", "check_password_reset_rate_limit"}:
        email = _text(body, "email", "p_email").lower()
        since = datetime.now(timezone.utc) - timedelta(minutes=15)
        clauses = [LoginAttempt.email == email, LoginAttempt.created_at >= since]
        if function_name == "check_login_rate_limit":
            clauses.append(LoginAttempt.succeeded.is_(False))
            maximum = get_settings().login_rate_limit
        else:
            clauses.append(LoginAttempt.detail == "password_reset_request")
            maximum = get_settings().password_reset_rate_limit
        count = int(
            (
                await session.execute(
                    select(func.count()).select_from(LoginAttempt).where(*clauses)
                )
            ).scalar_one()
        )
        return count < maximum
    if function_name == "is_identity_banned":
        model = MODEL_BY_TABLE["security_events"]
        identity = _text(body, "identity", "email", "phone", "p_identity").lower()
        result = await session.execute(
            select(func.count())
            .select_from(model)
            .where(
                model.type.in_(["identity_banned", "account_banned"]),
                model.status == "active",
                func.lower(func.coalesce(model.description, "")).contains(identity),
                model.deleted_at.is_(None),
            )
        )
        return int(result.scalar_one()) > 0
    if function_name == "get_product_likes_count":
        model = MODEL_BY_TABLE["product_likes"]
        product_id = _uuid(body.get("product_id") or body.get("p_product_id"), "product_id")
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.product_id == product_id, model.deleted_at.is_(None))
                )
            ).scalar_one()
        )
    if function_name in {"validate_coupon_for_checkout", "can_use_coupon"}:
        actor = _require_user(user)
        payload = await _coupon_payload(session, body, actor)
        return payload if function_name == "validate_coupon_for_checkout" else payload["valid"]
    if function_name == "increment_coupon_usage":
        _require_user(user)
        raise HTTPException(status_code=410, detail="coupon_usage_checkout_only")
    if function_name == "redeem_loyalty_points":
        _require_user(user)
        raise HTTPException(status_code=410, detail="loyalty_redeem_checkout_only")
    if function_name in {"open_operational_day", "close_operational_day"}:
        actor = _require_user(user)
        _require_roles(roles, STAFF_ROLES)
        model = MODEL_BY_TABLE["operational_days"]
        day = _text(body, "date", "p_date", default=datetime.now(timezone.utc).date().isoformat())
        row = (
            await session.execute(
                select(model)
                .where(model.extra_data["date"].astext == day, model.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            row = model(user_id=actor.id, status="open", description="يوم تشغيل", extra_data={"date": day})
            session.add(row)
        if function_name == "close_operational_day":
            pending = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Order)
                        .where(Order.status.in_(["pending", "processing"]), Order.deleted_at.is_(None))
                    )
                ).scalar_one()
            )
            if pending:
                return {"can_close": False, "pending_orders": pending}
            row.status = "closed"
            row.extra_data = {**(row.extra_data or {}), "closed_at": datetime.now(timezone.utc).isoformat()}
        await session.flush()
        return {"can_close": True, "day": serialize_record(row)}
    if function_name == "approve_partner_application":
        actor = _require_user(user)
        _require_roles(roles, ADMIN_ROLES)
        return await _approve_partner(session, body, actor)
    if function_name == "create_order_delay_ticket":
        actor = _require_user(user)
        order_id = _uuid(body.get("order_id") or body.get("p_order_id"), "order_id")
        order = await session.get(Order, order_id)
        if order is None or (order.user_id != actor.id and not roles.intersection(STAFF_ROLES)):
            raise HTTPException(status_code=404, detail="order_not_found")
        target = _text(body, "target", "p_target", default="admin").lower()
        if target not in {"admin", "partner"}:
            raise HTTPException(status_code=422, detail="delay_ticket_target_invalid")
        message = _text(body, "message", "p_message")
        if len(message) < 3:
            raise HTTPException(status_code=422, detail="delay_ticket_message_required")
        partner_ids: list[uuid.UUID] = []
        if target == "partner":
            partner_rows = await session.execute(
                select(OrderItem.partner_id)
                .where(OrderItem.order_id == order.id, OrderItem.partner_id.is_not(None))
                .distinct()
            )
            partner_ids = list(partner_rows.scalars())
            if not partner_ids:
                raise HTTPException(status_code=422, detail="merchant_contact_unavailable")
        model = MODEL_BY_TABLE["support_tickets"]
        row = model(
            user_id=actor.id,
            subject=f"بلاغ تأخير الطلب {order.order_number}",
            status="open",
            description=message,
            extra_data={
                "order_id": str(order.id),
                "target": target,
                "partner_ids": [str(partner_id) for partner_id in partner_ids],
            },
        )
        session.add(row)
        await session.flush()
        message_model = MODEL_BY_TABLE["ticket_messages"]
        session.add(
            message_model(
                ticket_id=row.id,
                sender_id=actor.id,
                message=message,
                is_staff=False,
                extra_data={"created_from": "order_delay_ticket", "target": target},
            )
        )
        if partner_ids:
            notifications = NotificationService(session)
            for partner_id in partner_ids:
                await notifications.create_notification(
                    NotificationPayload(
                        user_id=partner_id,
                        title="رسالة عميل بخصوص طلب",
                        body=f"لديك رسالة بخصوص الطلب {order.order_number}.",
                        notification_type="support_ticket",
                        category="support",
                        priority="high",
                        entity_type="support_ticket",
                        entity_id=str(row.id),
                        order_id=order.id,
                        payload={"ticket_id": str(row.id), "target": "partner"},
                        created_by=actor.id,
                        source="order_delay_ticket",
                        deduplication_key=f"order-delay-ticket:{row.id}:{partner_id}",
                    )
                )
        return serialize_record(row)
    if function_name in {"create_user_notification", "create_user_notifications"}:
        actor = _require_user(user)
        raw_ids = body.get("user_ids") or body.get("p_user_ids")
        if raw_ids is None:
            raw_ids = [body.get("user_id") or body.get("p_user_id") or actor.id]
        if not isinstance(raw_ids, list):
            raw_ids = [raw_ids]
        target_ids = [_uuid(item, "user_id") for item in raw_ids]
        if any(item != actor.id for item in target_ids):
            _require_roles(roles, STAFF_ROLES)
        model = MODEL_BY_TABLE["notifications"]
        created = []
        for target_id in target_ids:
            row = model(
                user_id=target_id,
                recipient_id=target_id,
                order_id=_uuid(body.get("order_id") or body.get("p_order_id"), "order_id")
                if body.get("order_id") or body.get("p_order_id")
                else None,
                title=_text(body, "title", "p_title", default="إشعار"),
                body=_text(body, "message", "p_message", "body", "p_body"),
                message=_text(body, "message", "p_message", "body", "p_body"),
                type=_text(body, "type", "p_type", default="message"),
                status="new",
                is_read=False,
            )
            session.add(row)
            created.append(row)
        await session.flush()
        return [serialize_record(row) for row in created]
    if function_name == "create_admin_notification":
        _require_roles(roles, STAFF_ROLES)
        model = MODEL_BY_TABLE["admin_notifications"]
        row = model(
            user_id=user.id if user else None,
            title=_text(body, "title", "p_title", default="إشعار إداري"),
            message=_text(body, "message", "p_message"),
            body=_text(body, "message", "p_message"),
            type=_text(body, "type", "p_type", default="message"),
            status="new",
            is_read=False,
            extra_data={
                "reference_type": body.get("reference_type") or body.get("p_reference_type"),
                "reference_id": body.get("reference_id") or body.get("p_reference_id"),
            },
        )
        session.add(row)
        await session.flush()
        return serialize_record(row)
    if function_name in {"send-order-email", "send-partner-approval"}:
        _require_user(user)
        raise HTTPException(status_code=410, detail="arbitrary_message_not_allowed")
        actor = _require_user(user)
        if function_name == "send-partner-approval":
            _require_roles(roles, STAFF_ROLES)
        return await _queue_message(
            session,
            "email_outbox",
            user_id=actor.id,
            title=_text(body, "title", default="رسالة من رفاهية التسوق"),
            message=_text(body, "message", default="تم تحديث طلبك."),
            body=body,
        )
    if function_name == "whatsapp-notify":
        _require_user(user)
        raise HTTPException(status_code=410, detail="arbitrary_recipient_not_allowed")
        actor = _require_user(user)
        return await _queue_message(
            session,
            "whatsapp_outbox",
            user_id=actor.id,
            title=_text(body, "title", default="إشعار واتساب"),
            message=_text(body, "message", default="تم تحديث طلبك."),
            body=body,
        )
    if function_name == "notification-fanout":
        actor = _require_user(user)
        _require_roles(roles, ADMIN_ROLES)
        if not (body.get("userIds") or body.get("user_ids")):
            raise HTTPException(status_code=422, detail="recipients_required")
        _require_roles(roles, STAFF_ROLES)
        user_ids = body.get("userIds") or body.get("user_ids") or []
        if not user_ids:
            user_ids = list(
                (
                    await session.execute(
                        select(User.id).where(User.is_active.is_(True), User.deleted_at.is_(None))
                    )
                ).scalars()
            )
        service = NotificationService(session)
        title = _text(body, "title", default="إشعار")
        message = _text(body, "message", "body", "p_message", "p_body")
        notification_type = _text(body, "type", "notification_type", default="message")
        category = _text(body, "category", default="system")
        priority = _text(body, "priority", default="high")
        action_url = _text(body, "actionUrl", "action_url", "url", "deep_link") or None
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        created = []
        for target in user_ids:
            target_id = _uuid(target, "user_id")
            notification = await service.create_notification(
                NotificationPayload(
                    user_id=target_id,
                    title=title,
                    body=message,
                    notification_type=notification_type,
                    category=category,
                    priority=priority,
                    action_url=action_url,
                    payload=payload,
                    created_by=actor.id,
                    source="function_fanout",
                )
            )
            created.append(notification)
        return {"ok": True, "created": len(created)}
    if function_name == "create-partner-user":
        actor = _require_user(user)
        _require_roles(roles, ADMIN_ROLES)
        if body.get("application_id") or body.get("applicationId"):
            return await _approve_partner(session, body, actor)
        email = _text(body, "email").lower()
        if not email:
            raise HTTPException(status_code=400, detail="email_required")
        existing = (await session.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
        if existing is None:
            temporary = f"Tmp{secrets.token_urlsafe(18)}9"
            existing = User(email=email, password_hash=hash_password(temporary), password_must_reset=True)
            session.add(existing)
            await session.flush()
            session.add(Profile(id=existing.id, user_id=existing.id, email=email, full_name=_text(body, "business_name", "businessName", default="تاجر")))
        if await session.get(UserRole, {"user_id": existing.id, "role": "partner"}) is None:
            session.add(UserRole(user_id=existing.id, role="partner"))
        return {"ok": True, "user_id": str(existing.id), "password_reset_required": True}
    if function_name in {"request-account-deletion", "account-deletion"}:
        actor = _require_user(user)
        model = MODEL_BY_TABLE["account_deletion_requests"]
        row = model(user_id=actor.id, status="pending", reason=_text(body, "reason"))
        session.add(row)
        await session.flush()
        return serialize_record(row)
    if function_name in {"delete-account"}:
        actor = _require_user(user)
        target_id = _uuid(body.get("user_id") or body.get("userId") or actor.id, "user_id")
        if target_id != actor.id:
            _require_roles(roles, ADMIN_ROLES)
        target = await session.get(User, target_id, with_for_update=True)
        if target is None:
            raise HTTPException(status_code=404, detail="user_not_found")
        target.is_active = False
        target.deleted_at = datetime.now(timezone.utc)
        return {"ok": True}
    if function_name in {"import-products", "ai-enhanced-import"}:
        _require_roles(roles, ADMIN_ROLES | {"partner"})
        ai_ledger_id = None
        if function_name == "ai-enhanced-import":
            ai_ledger_id = await _reserve_ai_usage(
                function_name=function_name,
                body=body,
                user=user,
                roles=roles,
                session=session,
                request=request,
            )
        items = body.get("products") or body.get("items") or []
        if not isinstance(items, list):
            if ai_ledger_id is not None:
                await AIQuotaService(session).fail(ai_ledger_id, error_code_safe="products_list_required")
            raise HTTPException(status_code=400, detail="products_list_required")
        created = []
        for item in items:
            if not isinstance(item, dict):
                continue
            product = Product(
                name=_text(item, "name", "title", default="منتج مستورد"),
                name_en=_text(item, "name_en", "nameEn") or None,
                price=Decimal(str(item.get("price") or 0)),
                stock_quantity=max(int(item.get("stock_quantity") or item.get("stock") or 0), 0),
                is_active=True,
                approval_status="approved" if roles.intersection(ADMIN_ROLES) else "pending",
                partner_id=user.id if user and "partner" in roles and not roles.intersection(ADMIN_ROLES) else None,
                image_url=item.get("image_url") or item.get("imageUrl"),
                images=item.get("images") if isinstance(item.get("images"), list) else [],
                extra_data={key: value for key, value in item.items() if key not in {"name", "title", "name_en", "nameEn", "price", "stock_quantity", "stock", "image_url", "imageUrl", "images"}},
            )
            session.add(product)
            created.append(product)
        await session.flush()
        if ai_ledger_id is not None:
            await AIQuotaService(session).complete(ai_ledger_id, actual_tokens=0)
        return {"ok": True, "imported": len(created), "products": [serialize_record(item) for item in created]}
    if function_name in {"categorize-products", "generate-product-descriptions"}:
        _require_roles(roles, ADMIN_ROLES | {"partner"})
        ai_ledger_id = await _reserve_ai_usage(
            function_name=function_name,
            body=body,
            user=user,
            roles=roles,
            session=session,
            request=request,
        )
        product_ids = body.get("productIds") or body.get("product_ids") or []
        statement = select(Product).where(Product.deleted_at.is_(None))
        if product_ids:
            statement = statement.where(Product.id.in_([_uuid(item, "product_id") for item in product_ids]))
        if user and "partner" in roles and not roles.intersection(ADMIN_ROLES):
            statement = statement.where(Product.partner_id == user.id)
        products = list((await session.execute(statement.limit(1000))).scalars())
        for product in products:
            if function_name == "generate-product-descriptions" and not product.description:
                product.description = f"اكتشف {product.name} ضمن منتجات رفاهية التسوق."
            if function_name == "categorize-products" and not product.tags:
                product.tags = [word for word in product.name.split()[:4]]
        await AIQuotaService(session).complete(ai_ledger_id, actual_tokens=0)
        return {"ok": True, "updated": len(products)}
    if function_name == "image-search":
        from .image_search import search_catalog_image

        return await search_catalog_image(body, session)
    if function_name == "product-images":
        ai_ledger_id = None
        if user is not None or function_name != "image-search":
            ai_ledger_id = await _reserve_ai_usage(
                function_name=function_name,
                body=body,
                user=user,
                roles=roles,
                session=session,
                request=request,
            )
        products = list(
            (
                await session.execute(
                    select(Product)
                    .where(Product.is_active.is_(True), Product.deleted_at.is_(None))
                    .order_by(Product.is_featured.desc(), Product.created_at.desc())
                    .limit(24)
                )
            ).scalars()
        )
        if ai_ledger_id is not None:
            await AIQuotaService(session).complete(ai_ledger_id, actual_tokens=0)
        return {"matches": [serialize_record(item) for item in products], "configured": True, "request_id": current_request_id()}
    if function_name in {"ai-product-assistant", "ai-chat-support"}:
        ai_ledger_id = await _reserve_ai_usage(
            function_name=function_name,
            body=body,
            user=user,
            roles=roles,
            session=session,
            request=request,
        )
        question = _text(body, "question", "message")
        product_payload = body.get("product")
        product_payload = product_payload if isinstance(product_payload, dict) else {}

        def product_text(*names: str) -> str:
            return _text(product_payload, *names)

        product_name = _text(
            body,
            "productName",
            "product_name",
            "name",
            default=product_text("name", "display_name", "displayName"),
        )
        price = body.get("price")
        if price is None:
            price = product_payload.get("price") or product_payload.get("priceLabel")
        stock = body.get("stock") or body.get("stock_quantity")
        if stock is None:
            stock = product_payload.get("stockQuantity") or product_payload.get("stock_quantity")
        details = []
        if product_name:
            details.append(f"المنتج: {product_name}")
        name_en = product_text("nameEn", "name_en")
        if name_en and name_en != product_name:
            details.append(f"الاسم بالإنجليزية: {name_en}")
        if price is not None:
            details.append(f"السعر: {price}")
        original_price = product_payload.get("originalPrice") or product_payload.get("original_price")
        if original_price is not None:
            details.append(f"السعر قبل الخصم: {original_price}")
        if stock is not None:
            details.append(f"المخزون المتاح: {stock}")
        for label, value in (
            ("القسم", product_text("category", "categoryName", "category_name")),
            ("الماركة", product_text("brand", "brandName", "brand_name")),
            ("المورد", product_text("supplier", "supplierName", "supplier_name")),
            ("المتجر", product_text("store", "storeName", "store_name")),
            ("الوصف", product_text("description", "short_description", "rich_description")),
        ):
            if value:
                details.append(f"{label}: {value[:1200]}")

        variants = body.get("variants") or product_payload.get("variants") or []
        if isinstance(variants, list):
            available_colors: list[str] = []
            available_sizes: list[str] = []
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                available = variant.get("available")
                variant_stock = variant.get("stockQuantity") or variant.get("stock_quantity")
                if available is False or (available is None and variant_stock is not None and int(variant_stock or 0) <= 0):
                    continue
                color = _text(variant, "color", "name")
                size = _text(variant, "size")
                if color and color not in available_colors:
                    available_colors.append(color)
                if size and size not in available_sizes:
                    available_sizes.append(size)
            if available_colors:
                details.append(f"الألوان المتاحة: {', '.join(available_colors[:12])}")
            if available_sizes:
                details.append(f"المقاسات المتاحة: {', '.join(available_sizes[:12])}")
        context = "، ".join(details) if details else "لا توجد بيانات منتج إضافية."
        try:
            language = "en" if str(body.get("language") or body.get("locale") or "").lower().startswith("en") else "ar"
            answer = await _ai_answer(question, context, language=language)
            answer = _sanitize_public_chat_response(answer, language)
        except HTTPException as exc:
            detail = exc.detail
            error_code = detail.get("code") if isinstance(detail, dict) else str(detail)
            await AIQuotaService(session).fail(ai_ledger_id, error_code_safe=error_code)
            raise
        await AIQuotaService(session).complete(ai_ledger_id, actual_tokens=max(1, len(answer) // 4))
        return {
            "answer": answer,
            "question": question,
            "configured": True,
            "request_id": current_request_id(),
        }
    if function_name == "share-preview":
        return {"ok": True, "title": _text(body, "title", default="رفاهية التسوق"), "url": _text(body, "url", "link")}
    if function_name == "security-monitor":
        _require_roles(roles, ADMIN_ROLES)
        event_model = MODEL_BY_TABLE["security_events"]
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        count = int((await session.execute(select(func.count()).select_from(event_model).where(event_model.created_at >= since))).scalar_one())
        return {"ok": True, "events_last_24_hours": count}
    if function_name == "process-email-queue":
        _require_roles(roles, STAFF_ROLES)
        return {"ok": True, **await process_email_outbox(session)}
    raise HTTPException(status_code=501, detail=f"function_not_implemented:{function_name}")
