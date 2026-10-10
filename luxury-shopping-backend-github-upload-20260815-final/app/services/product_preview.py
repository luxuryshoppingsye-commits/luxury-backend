from __future__ import annotations

import asyncio
import json
import time
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException
from PIL import Image

from .remote_image import validate_image_url, public_image_address, download_product_image

_cache: dict[str, tuple[float, bytes, str]] = {}


class ProductImageMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.json_parts: list[str] | None = None
        self.json_documents: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and str(values.get("property") or values.get("name") or values.get("itemprop") or "").lower() in {"og:image", "og:image:secure_url", "twitter:image", "twitter:image:src", "image"}:
            if values.get("content"):
                self.images.append(values["content"])
        if tag == "link" and values.get("rel") == "image_src" and values.get("href"):
            self.images.append(values["href"])
        if tag == "script" and values.get("type") == "application/ld+json":
            self.json_parts = []

    def handle_data(self, data):
        if self.json_parts is not None:
            self.json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.json_parts is not None:
            self.json_documents.append("".join(self.json_parts))
            self.json_parts = None

    def image_candidates(self):
        candidates = []
        for document in self.json_documents:
            try:
                value = json.loads(document)
            except (ValueError, TypeError):
                continue
            records = value if isinstance(value, list) else [value]
            for record in records:
                if not isinstance(record, dict):
                    continue
                nodes = record.get("@graph", [record])
                for node in nodes if isinstance(nodes, list) else []:
                    if not isinstance(node, dict) or "Product" not in str(node.get("@type", "")):
                        continue
                    images = node.get("image", [])
                    for image in images if isinstance(images, list) else [images]:
                        candidate = image.get("url") if isinstance(image, dict) else image
                        if isinstance(candidate, str):
                            candidates.append(candidate)
        # Product metadata takes precedence over a site's default sharing image.
        candidates.extend(reversed(self.images))
        return list(dict.fromkeys(candidate.strip() for candidate in candidates if candidate.strip()))[:8]


async def product_page_metadata(value: str) -> tuple[str, str]:
    url = validate_image_url(value)
    try:
        async with asyncio.timeout(18):
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
                for _ in range(5):
                    address = await public_image_address(url.host)
                    async with client.stream("GET", url.copy_with(host=address), headers={"Host": url.host, "User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity"}, extensions={"sni_hostname": url.host}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise HTTPException(422, "product_preview_unavailable")
                            url = validate_image_url(urljoin(str(url), location))
                            continue
                        if response.status_code != 200:
                            raise HTTPException(422, "product_source_unavailable")
                        mime = response.headers.get("content-type", "").split(";")[0].lower().strip()
                        if mime not in {"text/html", "application/xhtml+xml"}:
                            raise HTTPException(422, "product_page_required")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 1024 * 1024:
                                raise HTTPException(413, "product_page_too_large")
                        return data.decode("utf-8", errors="replace"), str(url)
                raise HTTPException(422, "product_preview_unavailable")
    except (httpx.HTTPError, asyncio.TimeoutError):
        raise HTTPException(422, "product_source_unavailable") from None


def thumbnail(data: bytes) -> bytes:
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 20_000_000:
                raise ValueError()
            image.thumbnail((800, 800))
            output = BytesIO()
            image.convert("RGB").save(output, format="WEBP", quality=85)
            return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "invalid_product_preview_image") from None


async def product_preview_image(value: str) -> tuple[bytes, str]:
    url = str(validate_image_url(value))
    cached = _cache.get(url)
    if cached and cached[0] > time.monotonic():
        return cached[1], cached[2]
    try:
        async with asyncio.timeout(35):
            document, final_url = await product_page_metadata(url)
            parser = ProductImageMetadata()
            parser.feed(document)
            for candidate in parser.image_candidates():
                try:
                    data, _ = await download_product_image(urljoin(final_url, candidate), 8 * 1024 * 1024)
                    result = await asyncio.to_thread(thumbnail, data)
                except HTTPException:
                    continue
                if len(_cache) >= 96:
                    _cache.pop(next(iter(_cache)))
                _cache[url] = (time.monotonic() + 900, result, "image/webp")
                return result, "image/webp"
    except asyncio.TimeoutError:
        raise HTTPException(422, "product_source_unavailable") from None
    raise HTTPException(404, "product_preview_image_unavailable")
