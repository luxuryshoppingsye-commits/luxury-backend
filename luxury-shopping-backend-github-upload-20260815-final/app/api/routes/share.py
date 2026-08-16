from __future__ import annotations

import html
import json
import re
import uuid
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...database import get_session
from ...models.domain import Product
from ...services.catalog_policy import (
    _public_upload_url,
    build_public_product_rows,
    public_product_clauses,
    validate_public_product_or_404,
)
from ...services.public_read_cache import cache_key, public_read_cache
from ...storage.files import FileStorage


router = APIRouter(tags=["share"])
settings = get_settings()
MAX_SHARE_IMAGE_BYTES = 12 * 1024 * 1024
SHARE_IMAGE_CACHE_CONTROL = "public, max-age=86400, s-maxage=604800, immutable"


def _clean_text(value: Any, *, fallback: str = "") -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or fallback


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _share_api_origin() -> str:
    # The custom API hostname currently has a Cloudflare Worker that replaces
    # social-bot responses. Render's origin is public and bypasses that Worker.
    configured = str(settings.render_public_url or "").strip().rstrip("/")
    if not configured.startswith(("https://", "http://")):
        configured = str(settings.api_base_url or "").strip().rstrip("/")
    if configured.startswith(("https://", "http://")):
        return configured
    return "https://luxury-backend-xy9d.onrender.com"


def _frontend_origin() -> str:
    configured = str(settings.frontend_public_url or "").strip().rstrip("/")
    return configured or "https://luxuryshoppings.com"


def _identifier(value: str) -> str:
    identifier = unquote(str(value or "")).strip()
    if not identifier or len(identifier) > 160 or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise HTTPException(status_code=404, detail="product_not_found")
    return identifier


async def _share_product_payload(identifier: str, session: AsyncSession) -> dict[str, Any]:
    lookup_clauses = []
    try:
        lookup_clauses.append(Product.id == uuid.UUID(identifier))
    except ValueError:
        pass
    lookup_clauses.extend((Product.short_code == identifier, Product.sku == identifier))
    product = (
        await session.execute(
            select(Product)
            .where(or_(*lookup_clauses), *public_product_clauses(Product))
            .limit(1)
        )
    ).scalar_one_or_none()
    validate_public_product_or_404(product)
    rows = await build_public_product_rows(session, [product], include_variants=False)
    if not rows:
        raise HTTPException(status_code=404, detail="product_not_found")
    return rows[0]


async def _get_share_product(identifier: str, session: AsyncSession) -> dict[str, Any]:
    return await public_read_cache.get_or_set(
        cache_key("share-product", identifier=identifier),
        lambda: _share_product_payload(identifier, session),
    )


def _product_image_candidates(product: dict[str, Any]) -> list[str]:
    raw_values = [product.get("image_url"), product.get("primary_image")]
    raw_values.extend(product.get("images") or [])
    candidates: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        normalized = _public_upload_url(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)
    return candidates


def _allowed_remote_image_host() -> str | None:
    configured = str(settings.r2_public_base_url or "").strip()
    parsed = urlparse(configured)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return parsed.hostname.lower()


def _detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _local_image_path(source: str):
    prefix = "/uploads/"
    if not source.startswith(prefix):
        return None
    relative = source[len(prefix) :]
    if not FileStorage.is_public_relative_path(relative):
        return None
    root = settings.resolved_upload_dir.resolve()
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        return None
    return target if target.is_file() else None


async def _read_image(source: str) -> tuple[bytes, str] | None:
    local_path = _local_image_path(source)
    if local_path is not None:
        data = local_path.read_bytes()
        media_type = _detect_image_type(data)
        return (data, media_type) if media_type else None

    parsed = urlparse(source)
    allowed_host = _allowed_remote_image_host()
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() != allowed_host:
        return None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=False,
            headers={"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "User-Agent": "LuxuryShoppingShare/1.0"},
        ) as client:
            async with client.stream("GET", source) as upstream:
                if upstream.status_code != 200:
                    return None
                declared_size = int(upstream.headers.get("content-length") or 0)
                if declared_size > MAX_SHARE_IMAGE_BYTES:
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in upstream.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_SHARE_IMAGE_BYTES:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
    except (httpx.HTTPError, OSError, ValueError):
        return None
    media_type = _detect_image_type(data)
    return (data, media_type) if media_type else None


def _build_share_html(identifier: str, product: dict[str, Any]) -> str:
    api_origin = _share_api_origin()
    frontend_origin = _frontend_origin()
    encoded_identifier = quote(identifier, safe="")
    share_url = f"{api_origin}/share/products/{encoded_identifier}"
    product_url = f"{frontend_origin}/p/{encoded_identifier}"
    image_url = f"{share_url}/image"
    title = _clean_text(product.get("name") or product.get("name_en"), fallback="منتج من رفاهية التسوق")
    description = _clean_text(
        product.get("description") or product.get("short_description"),
        fallback=f"تسوق {title} من رفاهية التسوق بأسعار مميزة وشحن موثوق داخل اليمن.",
    )[:500]
    price = product.get("price")
    currency = _clean_text(product.get("currency_code"), fallback="YER")
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": title,
        "description": description,
        "image": [image_url],
        "url": product_url,
        "offers": {
            "@type": "Offer",
            "url": product_url,
            "price": str(price or ""),
            "priceCurrency": currency,
            "availability": "https://schema.org/InStock" if product.get("is_orderable") else "https://schema.org/OutOfStock",
        },
    }
    json_ld_text = json.dumps(json_ld, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    price_meta = ""
    if price is not None:
        price_meta = (
            f'<meta property="product:price:amount" content="{_escape(price)}">'
            f'<meta property="product:price:currency" content="{_escape(currency)}">'
        )
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)} | رفاهية التسوق</title>
  <meta name="description" content="{_escape(description)}">
  <meta name="robots" content="index,follow">
  <link rel="canonical" href="{_escape(product_url)}">
  <meta property="og:type" content="product">
  <meta property="og:url" content="{_escape(product_url)}">
  <meta property="og:site_name" content="رفاهية التسوق">
  <meta property="og:locale" content="ar_YE">
  <meta property="og:title" content="{_escape(title)} | رفاهية التسوق">
  <meta property="og:description" content="{_escape(description)}">
  <meta property="og:image" content="{_escape(image_url)}">
  <meta property="og:image:secure_url" content="{_escape(image_url)}">
  <meta property="og:image:alt" content="{_escape(title)}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{_escape(title)} | رفاهية التسوق">
  <meta name="twitter:description" content="{_escape(description)}">
  <meta name="twitter:image" content="{_escape(image_url)}">
  {price_meta}
  <script type="application/ld+json">{json_ld_text}</script>
  <meta http-equiv="refresh" content="0;url={_escape(product_url)}">
</head>
<body>
  <p>جاري فتح المنتج… <a href="{_escape(product_url)}">اضغط هنا للمتابعة</a></p>
  <script>window.location.replace({json.dumps(product_url)});</script>
</body>
</html>"""


@router.get("/share/products/{product_id}", response_class=Response)
async def share_product_page(product_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    identifier = _identifier(product_id)
    product = await _get_share_product(identifier, session)
    return Response(
        content=_build_share_html(identifier, product),
        media_type="text/html",
        headers={"Cache-Control": "public, max-age=60, s-maxage=300"},
    )


@router.get("/share/products/{product_id}/image")
async def share_product_image(product_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    identifier = _identifier(product_id)
    product = await _get_share_product(identifier, session)
    for source in _product_image_candidates(product):
        image = await _read_image(source)
        if image is not None:
            data, media_type = image
            return Response(content=data, media_type=media_type, headers={"Cache-Control": SHARE_IMAGE_CACHE_CONTROL})
    raise HTTPException(status_code=404, detail="product_image_not_found")


@router.head("/share/products/{product_id}/image")
async def share_product_image_head(product_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    return await share_product_image(product_id, session)
