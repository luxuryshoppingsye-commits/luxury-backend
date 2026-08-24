"""Verify that every active public product exposes a complete loadable image.

This is a read-only deployment check. It intentionally exercises the public
HTTP contract instead of inspecting database rows only: a URL can exist in a
row while the CDN returns the wrong bytes or an incomplete response.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlparse

import httpx
from PIL import Image
from io import BytesIO


MAX_BYTES = 12 * 1024 * 1024


def _mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _content_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _is_complete_image(data: bytes, media_type: str) -> bool:
    """Reject recognizable but truncated responses before Pillow sees them."""
    if media_type == "image/jpeg":
        return data.endswith(b"\xff\xd9")
    if media_type == "image/png":
        return data.endswith(b"\xaeB`\x82")
    if media_type == "image/gif":
        return data.endswith(b";")
    if media_type == "image/webp":
        if len(data) < 12:
            return False
        declared_riff_size = int.from_bytes(data[4:8], "little") + 8
        return declared_riff_size == len(data)
    return False


def _absolute(value: str, base_url: str) -> str:
    if value.startswith(("http://", "https://")):
        return value
    return f"{base_url.rstrip('/')}/{value.lstrip('/')}"


def _candidate_urls(item: dict) -> list[str]:
    values = [item.get("image_url"), item.get("imageUrl")]
    values.extend(item.get("images") or [])
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def verify(base_url: str) -> dict:
    with httpx.Client(
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=False,
        headers={"Accept": "application/json, image/*", "User-Agent": "CatalogImageVerifier/1.0"},
    ) as client:
        response = client.get(f"{base_url.rstrip('/')}/api/catalog/products", params={"limit": 500, "includeTotal": "false"})
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") or payload.get("data") or []
        failures: list[dict] = []
        checked = 0
        for item in items:
            if not isinstance(item, dict) or item.get("is_active") is False:
                continue
            candidates = _candidate_urls(item)
            if not candidates:
                failures.append({"id": item.get("id"), "name": item.get("name"), "reason": "missing_image"})
                continue
            loaded = False
            for candidate in candidates:
                url = _absolute(candidate, base_url)
                try:
                    image_response = client.get(url)
                    data = image_response.content
                    if image_response.status_code != 200 or not data or len(data) > MAX_BYTES:
                        continue
                    declared = _mime(data)
                    if declared is None:
                        continue
                    response_type = _content_type(image_response.headers.get("content-type"))
                    if response_type != declared or not _is_complete_image(data, declared):
                        continue
                    with Image.open(BytesIO(data)) as image:
                        image.verify()
                    checked += 1
                    loaded = True
                    break
                except Exception:
                    continue
            if not loaded:
                failures.append({"id": item.get("id"), "name": item.get("name"), "reason": "unloadable", "candidates": candidates})
        return {"base_url": base_url, "active_products": sum(1 for item in items if isinstance(item, dict) and item.get("is_active") is not False), "images_checked": checked, "failures": failures, "ok": not failures}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "https://api.luxuryshoppings.com"))
    args = parser.parse_args()
    try:
        report = verify(args.base_url)
    except Exception as error:
        print(json.dumps({"ok": False, "reason": "catalog_request_failed", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
