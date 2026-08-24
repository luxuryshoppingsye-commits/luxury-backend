"""Repair production catalogue invariants through the staff API.

The command is intentionally dry-run by default.  ``--apply`` is required for
mutations and ``--deactivate-missing-images`` is required before an active
product with no image can be hidden from the public catalogue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from io import BytesIO
from typing import Any

import requests
from PIL import Image, ImageFile


DEFAULT_BASE_URL = "https://api.luxuryshoppings.com"
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _product_text(product: dict[str, Any]) -> str:
    return _norm(" ".join(
        str(product.get(key) or "")
        for key in ("name", "name_en", "description", "rich_description")
    ))


def _category_ids(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(row.get("slug") or "").strip(): str(row["id"])
        for row in rows
        if row.get("id") and row.get("slug")
    }


def _infer_category_slug(product: dict[str, Any]) -> str | None:
    text = _product_text(product)

    # The order is deliberate: specific child categories must win over broad
    # words such as "dress", "set", or "women".
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("boys-clothing", ("أولادي", "اولادي", "اولاد")),
        ("girls-clothing", ("بناتي", "بنات", "طفلة")),
        ("kids", ("طقم طفال", "اطفال", "أطفال", "طفل")),
        ("electronics-accessories", ("جراب جوال", "ايفون", "iphone", "هاتف")),
        ("home-appliances", ("شفاط", "أجهزة منزلية", "اجهزة منزلية")),
        ("home-kitchen-tools", ("زلابية", "كباسة", "كباسه")),
        ("sports-equipment", ("مسدس رش ماء", "رش ماء")),
        ("perfumes", ("عطر", "عطور", "برفيوم", "perfume")),
        ("men-bags", ("محفظة رجالية", "محفظه رجاليه", "حقيبة رجالية", "حقيبه رجاليه")),
        ("women-bags", ("حقيبة", "حقيبه", "شنطة", "شنطه")),
        ("women-dresses", ("فستان", "فساتين")),
        ("women-pajamas", ("بيجامه", "بيجامة")),
        ("women-lingerie", ("قميص نوم", "حماله صدر", "حمالة صدر", "حمالات سيليكون")),
        ("women-scarves", ("شال كشميري", "شال نسائي", "شال")),
        ("women-watches", ("ساعه", "ساعة", "ساعات")),
        ("women-makeup", ("أطافر", "اطافر", "أظافر", "اظافر", "ديرما", "لاصق جيلي")),
        ("women-accessories", ("مجوهرات", "سوار", "اساور", "اقراط", "أقراط", "بروش", "مشابك شعر", "ملصقات لولو", "ملصقات وشم")),
        ("men-accessories", ("قبعه", "قبعة", "جاكيت")),
        ("women-sets", ("بلوزه", "بلوزة", "تنوره", "تنورة")),
        ("home-kitchen", ("شريط مطاطي", "شريط مطاط")),
    )
    for slug, terms in rules:
        if any(term.lower() in text for term in terms):
            return slug
    return None


def _image_values(product: dict[str, Any]) -> list[str]:
    def usable(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        value = value.strip()
        return bool(value) and "placeholder" not in value.lower()

    values: list[str] = []
    for raw in [product.get("image_url"), product.get("imageUrl")]:
        if usable(raw):
            values.append(raw.strip())
    raw_images = product.get("images")
    if isinstance(raw_images, list):
        for raw in raw_images:
            if usable(raw):
                values.append(raw.strip())
            elif isinstance(raw, dict):
                for key in ("url", "image_url", "imageUrl", "path", "src"):
                    value = raw.get(key)
                    if usable(value):
                        values.append(value.strip())
                        break
    return list(dict.fromkeys(values))


def _absolute_image_url(value: str, base_url: str) -> str:
    if value.startswith(("http://", "https://")):
        return value
    return f"{base_url.rstrip('/')}/{value.lstrip('/')}"


def _image_mime(data: bytes) -> str | None:
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
    return value.split(";", 1)[0].strip().lower() if value else None


def _complete_image(data: bytes, media_type: str) -> bool:
    if media_type == "image/jpeg":
        return data.endswith(b"\xff\xd9")
    if media_type == "image/png":
        return data.endswith(b"\xaeB`\x82")
    if media_type == "image/gif":
        return data.endswith(b";")
    if media_type == "image/webp" and len(data) >= 12:
        return int.from_bytes(data[4:8], "little") + 8 == len(data)
    return False


def _repair_image_bytes(data: bytes, media_type: str) -> tuple[bytes, str] | None:
    if media_type != "image/jpeg":
        return None
    candidate = data if data.endswith(b"\xff\xd9") else data + b"\xff\xd9"
    previous = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(BytesIO(candidate)) as image:
            image.load()
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
            repaired = output.getvalue()
        return repaired, "image/jpeg"
    except Exception:
        return None
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous


def _find_repairable_image(
    session: requests.Session,
    product: dict[str, Any],
    base_url: str,
) -> tuple[bytes, str] | None:
    for value in _image_values(product):
        url = _absolute_image_url(value, base_url)
        try:
            response = session.get(url, timeout=45)
            data = response.content
            if response.status_code != 200 or not data or len(data) > MAX_IMAGE_BYTES:
                continue
            actual_mime = _image_mime(data)
            if actual_mime is None:
                continue
            response_mime = _content_type(response.headers.get("content-type"))
            if response_mime == actual_mime and _complete_image(data, actual_mime):
                continue
            repaired = _repair_image_bytes(data, actual_mime)
            if repaired is not None:
                return repaired
        except requests.RequestException:
            continue
    return None


def _upload_repaired_image(
    session: requests.Session,
    base_url: str,
    product_id: str,
    data: bytes,
) -> str:
    response = session.post(
        f"{base_url}/manage/product-image",
        files={"file": (f"product-{product_id}.jpg", data, "image/jpeg")},
        timeout=60,
    )
    response.raise_for_status()
    body = json.loads(response.content.decode("utf-8"))
    value = body.get("imageUrl") or body.get("url")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("image upload did not return imageUrl/url")
    return value.strip()


def _login(session: requests.Session, base_url: str, email: str, password: str) -> str:
    response = session.post(
        f"{base_url}/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    response.raise_for_status()
    body = json.loads(response.content.decode("utf-8"))
    token = body.get("access_token") or body.get("accessToken")
    if not token:
        raise RuntimeError("admin login did not return an access token")
    session.headers.update({"Authorization": f"Bearer {token}"})
    return str(token)


def _get_json(session: requests.Session, url: str, **params: Any) -> Any:
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return json.loads(response.content.decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--admin-email", default=os.getenv("ADMIN_EMAIL"))
    parser.add_argument("--admin-password", default=os.getenv("ADMIN_PASSWORD"))
    parser.add_argument("--apply", action="store_true", help="perform the planned API mutations")
    parser.add_argument(
        "--repair-cdn-images",
        action="store_true",
        help="repair active CDN images with MIME/EOF mismatches and upload canonical copies",
    )
    parser.add_argument(
        "--deactivate-missing-images",
        action="store_true",
        help="allow active products with no image to be deactivated",
    )
    args = parser.parse_args()

    if not args.admin_email or not args.admin_password:
        parser.error("ADMIN_EMAIL/ADMIN_PASSWORD or both credential flags are required")
    if args.deactivate_missing_images and not args.apply:
        parser.error("--deactivate-missing-images requires --apply")
    if args.repair_cdn_images and not args.apply:
        parser.error("--repair-cdn-images requires --apply")

    base_url = args.base_url.rstrip("/")
    session = requests.Session()
    _login(session, base_url, args.admin_email, args.admin_password)

    categories_body = _get_json(session, f"{base_url}/categories", limit=5000)
    categories = categories_body.get("data", categories_body) if isinstance(categories_body, dict) else categories_body
    category_ids = _category_ids(categories if isinstance(categories, list) else [])

    products_body = _get_json(session, f"{base_url}/api/catalog/admin/products", limit=2000)
    products = products_body.get("data", []) if isinstance(products_body, dict) else []
    if not isinstance(products, list):
        raise RuntimeError("admin products response did not contain a data list")

    plan: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unresolved_images: list[dict[str, Any]] = []
    for product in products:
        if product.get("deleted_at") is not None or product.get("deletedAt") is not None:
            continue
        active = product.get("is_active") is not False and product.get("isActive") is not False
        images = _image_values(product)
        changes: dict[str, Any] = {}
        if active and images and not _image_values({"image_url": product.get("image_url")}):
            changes["imageUrl"] = images[0]
            changes["images"] = images
        if active and not images:
            if not args.deactivate_missing_images:
                unresolved_images.append({"id": product.get("id"), "name": product.get("name")})
                continue
            changes["isActive"] = False
            changes["approvalStatus"] = "inactive"

        if active and not product.get("category_id") and not product.get("categoryId"):
            slug = _infer_category_slug(product)
            if slug and slug in category_ids:
                changes["categoryId"] = category_ids[slug]
                changes["categorySlug"] = slug
            elif changes.get("isActive") is not False:
                skipped.append({"id": product.get("id"), "name": product.get("name"), "reason": "no_confident_category"})

        if changes:
            plan.append({"id": product.get("id"), "name": product.get("name"), "changes": changes})

    if skipped and args.apply:
        print(json.dumps({"ok": False, "reason": "unclassified_products", "skipped": skipped}, ensure_ascii=False, indent=2))
        return 2

    applied: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    repaired_images: list[dict[str, Any]] = []
    if args.apply:
        for item in plan:
            changes = item.get("changes")
            if not isinstance(changes, dict):
                continue
            changes.pop("categorySlug", None)
            response = session.patch(
                f"{base_url}/manage/products/{item['id']}",
                json=changes,
                timeout=45,
            )
            if response.ok:
                applied.append({"id": item["id"], "name": item.get("name"), "changes": changes})
            else:
                failures.append({"id": item["id"], "status": response.status_code, "body": response.text[:500]})

    if args.repair_cdn_images:
        for product in products:
            if product.get("deleted_at") is not None or product.get("deletedAt") is not None:
                continue
            active = product.get("is_active") is not False and product.get("isActive") is not False
            if not active or not product.get("id"):
                continue
            repaired = _find_repairable_image(session, product, base_url)
            if repaired is None:
                continue
            try:
                image_url = _upload_repaired_image(session, base_url, str(product["id"]), repaired[0])
                response = session.patch(
                    f"{base_url}/manage/products/{product['id']}",
                    json={"imageUrl": image_url, "images": [image_url]},
                    timeout=45,
                )
                if response.ok:
                    repaired_images.append({"id": product["id"], "name": product.get("name"), "imageUrl": image_url})
                else:
                    failures.append({"id": product["id"], "status": response.status_code, "body": response.text[:500]})
            except (requests.RequestException, RuntimeError) as error:
                failures.append({"id": product["id"], "name": product.get("name"), "reason": str(error)})

    report = {
        "base_url": base_url,
        "mode": "apply" if args.apply else "dry-run",
        "products_read": len(products),
        "planned": plan,
        "unresolved_images": unresolved_images,
        "skipped": skipped,
        "applied": applied,
        "repaired_images": repaired_images,
        "failures": failures,
        "ok": not unresolved_images and not skipped and not failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
