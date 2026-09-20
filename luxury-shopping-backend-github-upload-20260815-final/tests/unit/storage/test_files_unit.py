from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.app.services import payment_refund_security
from backend.app.services.image_pipeline import prepare_image_upload
from backend.app.storage.files import FileStorage, LocalSignatureScanner


PNG_BYTES = b"\x89PNG\r\n\x1a\npng"


@pytest.mark.parametrize(
    ("data", "extension", "content_type"),
    [
        (b"\xff\xd8\xff\xe0jpeg", ".jpg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\npng", ".png", "image/png"),
        (b"GIF89agif", ".gif", "image/gif"),
        (b"RIFF1234WEBPwebp", ".webp", "image/webp"),
        (b"%PDF-1.7", ".pdf", "application/pdf"),
    ],
)
def test_detect_file_type_from_magic_bytes(data: bytes, extension: str, content_type: str) -> None:
    assert FileStorage._detect(data) == (extension, content_type)


@pytest.mark.parametrize("data", [b"", b"hello world", b"RIFF1234NOPE"])
def test_detect_rejects_unsupported_signatures(data: bytes) -> None:
    with pytest.raises(HTTPException) as exc_info:
        FileStorage._detect(data)

    assert exc_info.value.status_code == 415
    assert exc_info.value.detail == "unsupported_file_type"


def test_unknown_upload_purpose_is_rejected_before_path_creation() -> None:
    storage = object.__new__(FileStorage)
    storage.root = Path("/tmp/luxury-unit-uploads").resolve()

    with pytest.raises(HTTPException) as exc_info:
        storage.save_bytes("../outside", "image.png", b"\x89PNG\r\n\x1a\npng", "http://api.test")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "unsupported_upload_purpose"


def test_base64_uploads_are_disabled() -> None:
    storage = object.__new__(FileStorage)

    with pytest.raises(HTTPException) as exc_info:
        storage.save_base64("avatars", "avatar.png", "not-base64!!!", "http://api.test")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "base64_upload_not_allowed"


def test_public_upload_uses_generated_path_and_public_url(tmp_path: Path) -> None:
    storage = object.__new__(FileStorage)
    storage.root = tmp_path.resolve()
    storage.settings = SimpleNamespace(max_upload_bytes=10 * 1024 * 1024)
    storage.scanner = LocalSignatureScanner()

    stored = storage.save_bytes(
        "product_image",
        "../unsafe-name.png",
        PNG_BYTES,
        "http://api.test",
        roles={"admin"},
    )

    assert stored.relative_path.startswith("products/")
    assert ".." not in stored.relative_path
    assert stored.public_url == f"http://api.test/uploads/{stored.relative_path}"
    assert (tmp_path / stored.relative_path).is_file()


@pytest.mark.parametrize("role", ["employee", "logistics"])
@pytest.mark.parametrize("purpose", ["product_image", "site_asset"])
def test_catalog_staff_roles_can_upload_product_images(
    tmp_path: Path, role: str, purpose: str
) -> None:
    storage = object.__new__(FileStorage)
    storage.root = tmp_path.resolve()
    storage.settings = SimpleNamespace(max_upload_bytes=10 * 1024 * 1024)
    storage.scanner = LocalSignatureScanner()

    stored = storage.save_bytes(
        purpose,
        "catalog-item.png",
        PNG_BYTES,
        "http://api.test",
        roles={role},
    )

    expected_directory = "products/" if purpose == "product_image" else "site-assets/"
    assert stored.relative_path.startswith(expected_directory)
    assert stored.public_url == f"http://api.test/uploads/{stored.relative_path}"


def test_product_description_attachment_accepts_public_pdf(tmp_path: Path) -> None:
    storage = object.__new__(FileStorage)
    storage.root = tmp_path.resolve()
    storage.settings = SimpleNamespace(max_upload_bytes=10 * 1024 * 1024)
    storage.scanner = LocalSignatureScanner()

    stored = storage.save_bytes(
        "product-description-attachments",
        "manual.pdf",
        b"%PDF-1.7\nproduct manual",
        "http://api.test",
        roles={"employee"},
    )

    assert stored.relative_path.startswith("product-description/")
    assert stored.public_url == f"http://api.test/uploads/{stored.relative_path}"
    assert stored.content_type == "application/pdf"


def test_public_upload_can_write_to_r2_and_returns_r2_url(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class FakeR2Client:
        def put_object(self, **kwargs):
            calls.append(kwargs)

    fake_boto3 = SimpleNamespace(client=lambda *args, **kwargs: FakeR2Client())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    storage = object.__new__(FileStorage)
    storage.root = tmp_path.resolve()
    storage.settings = SimpleNamespace(
        max_upload_bytes=10 * 1024 * 1024,
        storage_provider="r2",
        r2_endpoint_url="https://account.r2.cloudflarestorage.com",
        r2_bucket="luxury-images-prod",
        r2_access_key_id="access-key",
        r2_secret_access_key="secret-key",
        r2_region="auto",
        r2_public_base_url="https://images.luxuryshoppings.com",
    )
    storage.scanner = LocalSignatureScanner()

    stored = storage.save_bytes(
        "product_image",
        "product.png",
        PNG_BYTES,
        "http://api.test",
        roles={"admin"},
    )

    assert stored.storage_provider == "cloudflare_r2"
    assert stored.storage_bucket == "luxury-images-prod"
    assert stored.public_url == f"https://images.luxuryshoppings.com/{stored.relative_path}"
    assert calls[0]["Bucket"] == "luxury-images-prod"
    assert calls[0]["Key"] == stored.relative_path
    assert not (tmp_path / stored.relative_path).exists()
    assert stored.quarantine_path == ""


@pytest.mark.parametrize(
    ("policy_key", "file_name"),
    [
        ("customer_request_attachment", "request.png"),
        ("payment_receipt", "receipt.png"),
    ],
)
def test_private_customer_files_use_durable_r2_storage_without_public_url(
    monkeypatch,
    tmp_path: Path,
    policy_key: str,
    file_name: str,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeR2Client:
        def put_object(self, **kwargs):
            calls.append(kwargs)

    fake_boto3 = SimpleNamespace(client=lambda *args, **kwargs: FakeR2Client())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    storage = object.__new__(FileStorage)
    storage.root = tmp_path.resolve()
    storage.settings = SimpleNamespace(
        max_upload_bytes=10 * 1024 * 1024,
        storage_provider="r2",
        r2_endpoint_url="https://account.r2.cloudflarestorage.com",
        r2_bucket="luxury-private-assets",
        r2_access_key_id="access-key",
        r2_secret_access_key="secret-key",
        r2_region="auto",
        r2_public_base_url="https://images.luxuryshoppings.com",
    )
    storage.scanner = LocalSignatureScanner()

    stored = storage.save_bytes(
        policy_key,
        file_name,
        PNG_BYTES,
        "http://api.test",
        roles={"customer"},
    )

    assert stored.storage_provider == "cloudflare_r2"
    assert stored.public_url is None
    assert stored.quarantine_path == ""
    assert calls[0]["Bucket"] == "luxury-private-assets"
    assert calls[0]["Key"] == stored.relative_path
    assert calls[0]["CacheControl"] == "private, max-age=0, no-store"
    assert not (tmp_path / stored.relative_path).exists()


def test_private_policy_is_not_publicly_servable(tmp_path: Path) -> None:
    storage = object.__new__(FileStorage)
    storage.root = tmp_path.resolve()
    storage.settings = SimpleNamespace(max_upload_bytes=10 * 1024 * 1024)
    storage.scanner = LocalSignatureScanner()

    stored = storage.save_bytes(
        "payment_receipt",
        "receipt.png",
        PNG_BYTES,
        "http://api.test",
        roles={"customer"},
    )

    assert stored.relative_path.startswith("_private/payment-receipts/")
    assert stored.public_url is None
    assert FileStorage.is_public_relative_path(stored.relative_path) is False


def test_private_receipt_can_be_streamed_from_r2() -> None:
    class FakeBody(io.BytesIO):
        pass

    class FakeR2Client:
        def get_object(self, **kwargs):
            assert kwargs["Bucket"] == "luxury-private-assets"
            assert kwargs["Key"] == "_private/payment-receipts/receipt.webp"
            return {"Body": FakeBody(b"receipt-bytes")}

    storage = object.__new__(FileStorage)
    storage.settings = SimpleNamespace(r2_bucket="luxury-private-assets")
    storage._r2_client_cache = FakeR2Client()

    response = payment_refund_security._inline_r2_receipt_response(
        storage,
        storage_key="_private/payment-receipts/receipt.webp",
        media_type="image/webp",
    )

    async def collect_body() -> bytes:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    assert asyncio.run(collect_body()) == b"receipt-bytes"
    assert response.media_type == "image/webp"
    assert response.headers["content-disposition"] == "inline"


def test_eicar_upload_is_quarantined_and_rejected(tmp_path: Path) -> None:
    storage = object.__new__(FileStorage)
    storage.root = tmp_path.resolve()
    storage.settings = SimpleNamespace(max_upload_bytes=10 * 1024 * 1024)
    storage.scanner = LocalSignatureScanner()
    infected = b"%PDF-1.7\nX5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!"

    with pytest.raises(HTTPException) as exc_info:
        storage.save_bytes("payment_receipt", "receipt.pdf", infected, "http://api.test", roles={"customer"})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "malware_or_active_content_detected"
    quarantined = list((tmp_path / "_quarantine" / "payment-receipts").glob("*.upload"))
    assert len(quarantined) == 1
    assert not (tmp_path / "_private" / "payment-receipts").exists()


def test_image_pipeline_outputs_webp_and_preserves_receipt_content_policy() -> None:
    source = Image.new("RGB", (420, 300), (180, 120, 60))
    source_bytes = io.BytesIO()
    source.save(source_bytes, format="PNG")
    settings = SimpleNamespace(
        image_max_dimension=2400,
        image_min_dimension=900,
        image_ai_enhancement_enabled=False,
        image_ai_model="gemini-2.5-flash-image",
        image_ai_max_input_bytes=5 * 1024 * 1024,
        image_ai_timeout_seconds=5,
        gemini_api_key="",
        google_api_key="",
        ai_api_key="",
    )

    result = asyncio.run(
        prepare_image_upload(
            source_bytes.getvalue(),
            "receipt.png",
            "image/png",
            policy_key="payment_receipt",
            max_bytes=5 * 1024 * 1024,
            settings=settings,
        )
    )

    assert result.content_type == "image/webp"
    assert result.filename == "receipt.webp"
    assert result.provider == "local_webp"
    assert result.enhanced is False
    assert result.width > 420
    assert result.height > 300
    with Image.open(io.BytesIO(result.data)) as normalized:
        assert normalized.format == "WEBP"
        assert normalized.size == (result.width, result.height)


def test_image_pipeline_rejects_non_image_payloads() -> None:
    with pytest.raises(ValueError, match="not_an_image"):
        asyncio.run(
            prepare_image_upload(
                b"%PDF-1.7",
                "invoice.pdf",
                "application/pdf",
                policy_key="merchant_document",
                max_bytes=5 * 1024 * 1024,
                settings=SimpleNamespace(),
            )
        )
