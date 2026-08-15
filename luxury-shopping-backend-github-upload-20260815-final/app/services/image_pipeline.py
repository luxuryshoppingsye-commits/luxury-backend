from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageEnhance, ImageOps, ImageFilter

from ..config import Settings, get_settings


logger = logging.getLogger(__name__)

# Generative editing is deliberately limited to photos where changing pixels is
# acceptable. Receipts, identity documents, and other evidence are normalized
# locally only so their text and financial/legal content cannot be changed.
GEMINI_SAFE_POLICIES = frozenset(
    {
        "avatar",
        "product_image",
        "product_variant_image",
        "site_asset",
        "merchant_asset",
    }
)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"})


@dataclass(frozen=True)
class ImagePipelineResult:
    data: bytes
    filename: str
    content_type: str
    width: int
    height: int
    original_size_bytes: int
    enhanced: bool
    provider: str


def _looks_like_image(filename: str, content_type: str | None) -> bool:
    normalized = (content_type or "").strip().lower()
    return normalized.startswith("image/") or Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def _safe_filename(filename: str) -> str:
    raw = Path(str(filename or "image").replace("\\", "/")).name
    stem = Path(raw).stem or "image"
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in stem)
    return f"{safe[:150] or 'image'}.webp"


def _decode(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        return image.copy()


def _resize_and_enhance(image: Image.Image, *, max_dimension: int, min_dimension: int) -> Image.Image:
    largest = max(image.width, image.height)
    if largest <= 0:
        raise ValueError("invalid_image_dimensions")
    scale = 1.0
    if largest > max_dimension:
        scale = max_dimension / largest
    elif largest < min_dimension and largest >= 360:
        scale = min(1.8, min_dimension / largest)
    if abs(scale - 1.0) > 0.01:
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    image = ImageEnhance.Contrast(image).enhance(1.04)
    image = ImageEnhance.Color(image).enhance(1.02)
    image = ImageEnhance.Sharpness(image).enhance(1.12)
    return image.filter(ImageFilter.UnsharpMask(radius=1.0, percent=55, threshold=3))


def _encode_webp(image: Image.Image, quality: int) -> bytes:
    output = io.BytesIO()
    image.save(output, format="WEBP", quality=quality, method=6)
    return output.getvalue()


def _fit_size(image: Image.Image, *, max_bytes: int, max_dimension: int) -> tuple[bytes, Image.Image]:
    working = image
    for dimension_pass in range(5):
        for quality in (90, 86, 82, 78, 74, 68):
            data = _encode_webp(working, quality)
            if len(data) <= max_bytes:
                return data, working
        largest = max(working.width, working.height)
        next_dimension = min(max_dimension, round(largest * 0.82))
        if next_dimension >= largest:
            break
        working = working.resize(
            (max(1, round(working.width * next_dimension / largest)), max(1, round(working.height * next_dimension / largest))),
            Image.Resampling.LANCZOS,
        )
    raise ValueError("image_cannot_fit_upload_limit")


def _gemini_image_bytes(payload: dict[str, Any]) -> bytes | None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                try:
                    return base64.b64decode(inline["data"], validate=True)
                except (ValueError, TypeError):
                    return None
    return None


def _prompt_for(policy_key: str) -> str:
    if policy_key == "avatar":
        return (
            "Enhance this profile photo conservatively. Preserve the exact person's identity, face, clothing, colors, "
            "and composition. Improve lighting, sharpness, and compression only. Do not add, remove, or invent objects. "
            "Return exactly one edited image and no text."
        )
    return (
        "Enhance this ecommerce photo conservatively. Preserve the exact product or subject, shape, colors, branding, "
        "logos, text, and composition. Improve lighting, clarity, fine detail, and compression only. Do not add or remove "
        "objects and do not invent text. Return exactly one edited image and no text."
    )


async def _enhance_with_gemini(image_bytes: bytes, *, policy_key: str, settings: Settings) -> bytes | None:
    # Image generation must use the Gemini/Google credential specifically;
    # AI_API_KEY may belong to another provider used by text features.
    api_key = (settings.gemini_api_key or settings.google_api_key or settings.ai_api_key).strip()
    model = settings.image_ai_model.strip()
    if not settings.image_ai_enhancement_enabled or not api_key or not model:
        return None
    if len(image_bytes) > settings.image_ai_max_input_bytes:
        return None
    if api_key.startswith("ya29."):
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    else:
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": _prompt_for(policy_key)},
                {"inlineData": {"mimeType": "image/webp", "data": base64.b64encode(image_bytes).decode("ascii")}},
            ],
        }],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        # Image enhancement is an optional optimization inside the upload
        # request. Keep it well below the mobile API timeout so a slow or
        # temporarily unavailable Gemini endpoint can never make the actual
        # WebP upload fail. The local pipeline remains the reliable fallback.
        enhancement_timeout = min(settings.image_ai_timeout_seconds, 8)
        async with httpx.AsyncClient(timeout=enhancement_timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = _gemini_image_bytes(response.json())
            if result:
                # Validate that the response is really an image before it can
                # enter the storage pipeline.
                _decode(result)
                return result
    except Exception as exc:  # AI is an enhancement, never a reason to lose an upload.
        logger.warning("Gemini image enhancement unavailable: %s", exc)
    return None


async def prepare_image_upload(
    data: bytes,
    filename: str,
    content_type: str | None,
    *,
    policy_key: str,
    max_bytes: int,
    settings: Settings | None = None,
) -> ImagePipelineResult:
    settings = settings or get_settings()
    if not _looks_like_image(filename, content_type):
        raise ValueError("not_an_image")
    if not data:
        raise ValueError("empty_image")

    image = _resize_and_enhance(
        _decode(data),
        max_dimension=settings.image_max_dimension,
        min_dimension=settings.image_min_dimension,
    )
    local_bytes, local_image = _fit_size(
        image,
        max_bytes=max_bytes,
        max_dimension=settings.image_max_dimension,
    )
    enhanced = False
    provider = "local_webp"
    if policy_key in GEMINI_SAFE_POLICIES:
        generated = await _enhance_with_gemini(local_bytes, policy_key=policy_key, settings=settings)
        if generated:
            try:
                generated_image = _resize_and_enhance(
                    _decode(generated),
                    max_dimension=settings.image_max_dimension,
                    min_dimension=settings.image_min_dimension,
                )
                local_bytes, local_image = _fit_size(
                    generated_image,
                    max_bytes=max_bytes,
                    max_dimension=settings.image_max_dimension,
                )
                enhanced = True
                provider = "gemini"
            except (OSError, ValueError):
                logger.warning("Gemini returned an image that could not be normalized")

    return ImagePipelineResult(
        data=local_bytes,
        filename=_safe_filename(filename),
        content_type="image/webp",
        width=local_image.width,
        height=local_image.height,
        original_size_bytes=len(data),
        enhanced=enhanced,
        provider=provider,
    )
