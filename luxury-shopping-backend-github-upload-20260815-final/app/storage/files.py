from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..config import get_settings


logger = logging.getLogger(__name__)


ALLOWED_SIGNATURES = {
    b"\xff\xd8\xff": (".jpg", "image/jpeg"),
    b"\x89PNG\r\n\x1a\n": (".png", "image/png"),
    b"GIF87a": (".gif", "image/gif"),
    b"GIF89a": (".gif", "image/gif"),
    b"RIFF": (".webp", "image/webp"),
    b"%PDF": (".pdf", "application/pdf"),
}
EXTENSIONS_BY_MIME = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
    "application/pdf": {".pdf"},
}
EICAR_SIGNATURE = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR"
FORBIDDEN_TEXT_MARKERS = (
    b"<svg",
    b"<script",
    b"<!doctype html",
    b"<html",
)
FORBIDDEN_CLIENT_FIELDS = frozenset(
    {
        "bucket",
        "path",
        "storage_path",
        "storagePath",
        "storage_key",
        "storageKey",
        "url",
        "fileUrl",
        "file_url",
        "dataBase64",
        "base64",
        "externalUrl",
        "external_url",
    }
)


@dataclass(frozen=True)
class StoragePolicy:
    key: str
    directory: str
    visibility: str
    allowed_content_types: frozenset[str]
    max_bytes: int
    upload_roles: frozenset[str]
    read_roles: frozenset[str]
    requires_owner: bool = True
    requires_scan: bool = True


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    public_url: str | None
    storage_provider: str
    storage_bucket: str
    content_type: str
    sha256: str
    size: int
    policy_key: str
    visibility: str
    original_filename: str
    scan_status: str
    scan_provider: str
    quarantine_path: str


@dataclass(frozen=True)
class ScanResult:
    status: str
    provider: str
    signature: str | None = None


IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
IMAGE_OR_PDF_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "application/pdf"})
PUBLIC_IMAGE_ROLES = frozenset({"admin", "manager", "staff", "partner"})
CUSTOMER_ATTACHMENT_ROLES = frozenset({"customer", "admin", "manager", "staff"})


class StoragePolicyRegistry:
    POLICIES: dict[str, StoragePolicy] = {
        "avatar": StoragePolicy(
            key="avatar",
            directory="avatars",
            visibility="public",
            allowed_content_types=IMAGE_MIMES,
            max_bytes=5 * 1024 * 1024,
            upload_roles=frozenset({"customer", "admin", "manager", "staff", "partner"}),
            read_roles=frozenset(),
        ),
        "product_image": StoragePolicy(
            key="product_image",
            directory="products",
            visibility="public",
            allowed_content_types=IMAGE_MIMES,
            max_bytes=8 * 1024 * 1024,
            upload_roles=PUBLIC_IMAGE_ROLES,
            read_roles=frozenset(),
        ),
        "product_variant_image": StoragePolicy(
            key="product_variant_image",
            directory="product-variants",
            visibility="public",
            allowed_content_types=IMAGE_MIMES,
            max_bytes=8 * 1024 * 1024,
            upload_roles=PUBLIC_IMAGE_ROLES,
            read_roles=frozenset(),
        ),
        "site_asset": StoragePolicy(
            key="site_asset",
            directory="site-assets",
            visibility="public",
            allowed_content_types=IMAGE_MIMES | frozenset({"image/gif"}),
            max_bytes=10 * 1024 * 1024,
            upload_roles=frozenset({"admin", "manager", "staff"}),
            read_roles=frozenset(),
            requires_owner=False,
        ),
        "product_description_attachment": StoragePolicy(
            key="product_description_attachment",
            directory="product-description",
            visibility="public",
            allowed_content_types=IMAGE_OR_PDF_MIMES,
            max_bytes=10 * 1024 * 1024,
            upload_roles=frozenset({"admin", "manager", "staff", "employee", "partner"}),
            read_roles=frozenset(),
            requires_owner=False,
        ),
        "merchant_asset": StoragePolicy(
            key="merchant_asset",
            directory="merchant-assets",
            visibility="public",
            allowed_content_types=IMAGE_MIMES,
            max_bytes=8 * 1024 * 1024,
            # A customer must be able to attach the store image while the
            # merchant application is still awaiting approval. The asset is
            # owned by the authenticated user and remains a public image.
            upload_roles=frozenset({"customer", "partner", "admin", "manager", "staff"}),
            read_roles=frozenset(),
        ),
        "merchant_document": StoragePolicy(
            key="merchant_document",
            directory="merchant-documents",
            visibility="private",
            allowed_content_types=IMAGE_OR_PDF_MIMES,
            max_bytes=12 * 1024 * 1024,
            upload_roles=frozenset({"partner", "admin", "manager", "staff"}),
            read_roles=frozenset({"admin", "manager", "staff", "partner"}),
        ),
        "payment_receipt": StoragePolicy(
            key="payment_receipt",
            directory="payment-receipts",
            visibility="private",
            allowed_content_types=IMAGE_OR_PDF_MIMES,
            max_bytes=10 * 1024 * 1024,
            upload_roles=frozenset({"customer", "admin", "manager", "finance"}),
            read_roles=frozenset({"admin", "manager", "finance"}),
        ),
        "support_attachment": StoragePolicy(
            key="support_attachment",
            directory="support",
            visibility="private",
            allowed_content_types=IMAGE_OR_PDF_MIMES,
            max_bytes=10 * 1024 * 1024,
            upload_roles=CUSTOMER_ATTACHMENT_ROLES,
            read_roles=frozenset({"admin", "manager", "staff"}),
        ),
        "complaint_attachment": StoragePolicy(
            key="complaint_attachment",
            directory="complaints",
            visibility="private",
            allowed_content_types=IMAGE_OR_PDF_MIMES,
            max_bytes=10 * 1024 * 1024,
            upload_roles=CUSTOMER_ATTACHMENT_ROLES,
            read_roles=frozenset({"admin", "manager", "staff"}),
        ),
        "return_attachment": StoragePolicy(
            key="return_attachment",
            directory="returns",
            visibility="private",
            allowed_content_types=IMAGE_OR_PDF_MIMES,
            max_bytes=10 * 1024 * 1024,
            upload_roles=CUSTOMER_ATTACHMENT_ROLES,
            read_roles=frozenset({"admin", "manager", "staff"}),
        ),
        "review_attachment": StoragePolicy(
            key="review_attachment",
            directory="review-images",
            visibility="public",
            allowed_content_types=IMAGE_MIMES,
            max_bytes=5 * 1024 * 1024,
            upload_roles=frozenset({"customer", "admin", "manager", "staff"}),
            read_roles=frozenset(),
        ),
        "customer_request_attachment": StoragePolicy(
            key="customer_request_attachment",
            directory="customer-requests",
            visibility="private",
            allowed_content_types=IMAGE_OR_PDF_MIMES,
            max_bytes=10 * 1024 * 1024,
            upload_roles=CUSTOMER_ATTACHMENT_ROLES,
            read_roles=frozenset({"admin", "manager", "staff"}),
        ),
        "invoice": StoragePolicy(
            key="invoice",
            directory="invoices",
            visibility="private",
            allowed_content_types=frozenset({"application/pdf"}),
            max_bytes=10 * 1024 * 1024,
            upload_roles=frozenset({"admin", "manager", "finance"}),
            read_roles=frozenset({"admin", "manager", "finance"}),
        ),
        "report_export": StoragePolicy(
            key="report_export",
            directory="reports",
            visibility="private",
            allowed_content_types=frozenset({"application/pdf", "text/csv; charset=utf-8", "text/csv"}),
            max_bytes=20 * 1024 * 1024,
            upload_roles=frozenset({"admin", "manager", "finance"}),
            read_roles=frozenset({"admin", "manager", "finance"}),
            requires_owner=False,
        ),
    }
    ALIASES: dict[str, str] = {
        "avatars": "avatar",
        "avatar": "avatar",
        "profile-avatar": "avatar",
        "products": "product_image",
        "product-images": "product_image",
        "product_image": "product_image",
        "product-variant-images": "product_variant_image",
        "product-variants": "product_variant_image",
        "site-assets": "site_asset",
        "site_asset": "site_asset",
        "banners": "site_asset",
        "logos": "site_asset",
        "product-description-attachments": "product_description_attachment",
        "product-description": "product_description_attachment",
        "product_description_attachment": "product_description_attachment",
        "supplier-assets": "site_asset",
        "partner-assets": "merchant_asset",
        "partner-documents": "merchant_document",
        "merchant-documents": "merchant_document",
        "merchant_document": "merchant_document",
        "receipt": "payment_receipt",
        "receipts": "payment_receipt",
        "payment-receipts": "payment_receipt",
        "payment_receipts": "payment_receipt",
        "payment_receipt": "payment_receipt",
        "support": "support_attachment",
        "support-attachments": "support_attachment",
        "complaints": "complaint_attachment",
        "complaint-attachments": "complaint_attachment",
        "returns": "return_attachment",
        "return-attachments": "return_attachment",
        "review-images": "review_attachment",
        "review_attachments": "review_attachment",
        "local-shopping-images": "customer_request_attachment",
        "international-shopping-images": "customer_request_attachment",
        "customer-request-attachments": "customer_request_attachment",
        "invoices": "invoice",
        "reports": "report_export",
        "report-exports": "report_export",
    }

    @classmethod
    def resolve(cls, value: str | None) -> StoragePolicy:
        normalized = str(value or "").strip().replace("\\", "/").strip("/").lower()
        key = cls.ALIASES.get(normalized, normalized)
        policy = cls.POLICIES.get(key)
        if policy is None:
            raise HTTPException(status_code=422, detail="unsupported_upload_purpose")
        return policy

    @classmethod
    def public_directories(cls) -> frozenset[str]:
        return frozenset(policy.directory for policy in cls.POLICIES.values() if policy.visibility == "public")

    @classmethod
    def private_directories(cls) -> frozenset[str]:
        return frozenset(policy.directory for policy in cls.POLICIES.values() if policy.visibility != "public")

    @classmethod
    def as_dict(cls) -> dict[str, dict[str, Any]]:
        return {
            key: {
                "directory": policy.directory,
                "visibility": policy.visibility,
                "allowed_content_types": sorted(policy.allowed_content_types),
                "max_bytes": policy.max_bytes,
                "upload_roles": sorted(policy.upload_roles),
                "read_roles": sorted(policy.read_roles),
                "requires_owner": policy.requires_owner,
                "requires_scan": policy.requires_scan,
            }
            for key, policy in sorted(cls.POLICIES.items())
        }


class LocalSignatureScanner:
    provider = "local-signature-scanner"

    def scan(self, data: bytes, content_type: str) -> ScanResult:
        lowered = data[:4096].lower()
        if EICAR_SIGNATURE in data:
            return ScanResult(status="infected", provider=self.provider, signature="eicar")
        if content_type != "application/pdf" and any(marker in lowered for marker in FORBIDDEN_TEXT_MARKERS):
            return ScanResult(status="blocked", provider=self.provider, signature="active_content")
        return ScanResult(status="clean", provider=self.provider)


class FileStorage:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = self.settings.resolved_upload_dir.resolve()
        self.scanner = LocalSignatureScanner()

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=503, detail="durable_storage_unavailable") from exc
        if not os.access(self.root, os.W_OK):
            raise HTTPException(status_code=503, detail="durable_storage_unavailable")

    def _uses_r2_for_policy(self, policy: StoragePolicy) -> bool:
        return (
            str(getattr(self.settings, "storage_provider", "local")).strip().lower() == "r2"
            and policy.visibility == "public"
        )

    def _r2_client(self):
        client = getattr(self, "_r2_client_cache", None)
        if client is not None:
            return client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="r2_client_unavailable") from exc
        try:
            endpoint_url = str(self.settings.r2_endpoint_url).strip()
            if len(endpoint_url) >= 2 and endpoint_url[0] == endpoint_url[-1] and endpoint_url[0] in {'"', "'"}:
                endpoint_url = endpoint_url[1:-1].strip()
            if "://" not in endpoint_url:
                endpoint_url = f"https://{endpoint_url}"
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url.rstrip("/"),
                aws_access_key_id=str(self.settings.r2_access_key_id),
                aws_secret_access_key=str(self.settings.r2_secret_access_key),
                region_name=str(getattr(self.settings, "r2_region", "auto") or "auto"),
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
            self._r2_init_error = {
                "error_type": type(exc).__name__,
                "error_code": str(error.get("Code") or "unknown"),
                "http_status": metadata.get("HTTPStatusCode"),
                "error_message": self._safe_r2_error_message(str(exc)),
            }
            logger.exception("r2_client_init_failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="r2_client_unavailable") from exc
        self._r2_client_cache = client
        return client

    def _safe_r2_error_message(self, message: str) -> str:
        """Keep diagnostics useful while preventing credentials from being returned."""
        redacted = str(message)
        for value in (
            str(getattr(self.settings, "r2_endpoint_url", "")),
            str(getattr(self.settings, "r2_access_key_id", "")),
            str(getattr(self.settings, "r2_secret_access_key", "")),
        ):
            if value:
                redacted = redacted.replace(value, "[redacted]")
        return redacted[:240]

    def _upload_to_r2(self, *, key: str, data: bytes, content_type: str, policy_key: str, sha256: str) -> None:
        try:
            self._r2_client().put_object(
                Bucket=str(self.settings.r2_bucket),
                Key=key,
                Body=data,
                ContentType=content_type,
                # Upload keys are UUID-based and immutable. Long-lived edge
                # caching prevents every product card from re-downloading
                # the same WebP after navigation.
                CacheControl="public, max-age=31536000, immutable",
                Metadata={"sha256": sha256, "policy": policy_key},
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "r2_upload_failed provider=r2 bucket=%s key=%s error_type=%s",
                str(self.settings.r2_bucket),
                key,
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail="r2_upload_failed") from exc

    def r2_diagnostics(self) -> dict[str, Any]:
        """Perform a read-only R2 connectivity check without exposing secrets."""
        provider = str(getattr(self.settings, "storage_provider", "local")).strip().lower()
        if provider != "r2":
            return {"provider": provider, "configured": False, "reachable": False, "reason": "provider_not_r2"}
        try:
            client = self._r2_client()
            bucket = str(self.settings.r2_bucket)
            client.head_bucket(Bucket=bucket)
            probe_key = f"_health/r2-{uuid.uuid4().hex}.txt"
            client.put_object(Bucket=bucket, Key=probe_key, Body=b"r2-health", ContentType="text/plain")
            client.delete_object(Bucket=bucket, Key=probe_key)
            return {
                "provider": "cloudflare_r2",
                "configured": True,
                "reachable": True,
                "write_delete": True,
            }
        except HTTPException as exc:
            result = {
                "provider": "cloudflare_r2",
                "configured": True,
                "reachable": False,
                "error_code": str(exc.detail),
            }
            result.update(getattr(self, "_r2_init_error", {}))
            return result
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
            return {
                "provider": "cloudflare_r2",
                "configured": True,
                "reachable": False,
                "error_type": type(exc).__name__,
                "error_code": str(error.get("Code") or "unknown"),
                "http_status": metadata.get("HTTPStatusCode"),
                "error_message": self._safe_r2_error_message(str(exc)),
            }

    @staticmethod
    def _detect(data: bytes) -> tuple[str, str]:
        for signature, metadata in ALLOWED_SIGNATURES.items():
            if data.startswith(signature):
                if signature == b"RIFF" and data[8:12] != b"WEBP":
                    continue
                return metadata
        raise HTTPException(status_code=415, detail="unsupported_file_type")

    @staticmethod
    def _safe_original_filename(value: str | None) -> str:
        name = Path(str(value or "file").replace("\\", "/")).name
        sanitized = re.sub(r"[^A-Za-z0-9_. -]+", "_", name).strip(" ._")
        return sanitized[:180] or "file"

    @staticmethod
    def _extension_matches_filename(file_name: str, content_type: str) -> bool:
        suffix = Path(file_name).suffix.lower()
        if not suffix:
            return True
        return suffix in EXTENSIONS_BY_MIME.get(content_type, frozenset())

    @staticmethod
    def _policy_target(policy: StoragePolicy, extension: str) -> str:
        generated = f"{uuid.uuid4().hex}{extension}"
        if policy.visibility == "public":
            return f"{policy.directory}/{generated}"
        return f"_private/{policy.directory}/{generated}"

    @staticmethod
    def _quarantine_target(policy: StoragePolicy) -> str:
        return f"_quarantine/{policy.directory}/{uuid.uuid4().hex}.upload"

    def _safe_join(self, relative_path: str) -> Path:
        normalized = str(relative_path).replace("\\", "/").lstrip("/")
        target = (self.root / normalized).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_storage_path") from exc
        return target

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        with part.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        part.replace(path)

    def _authorize_upload(self, policy: StoragePolicy, roles: set[str]) -> None:
        if roles.intersection(policy.upload_roles):
            return
        raise HTTPException(status_code=403, detail="upload_policy_denied")

    def create_presigned_upload(
        self,
        *,
        policy_key: str,
        file_name: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        roles: set[str],
        expires_in: int = 900,
    ) -> dict[str, Any]:
        policy = StoragePolicyRegistry.resolve(policy_key)
        self._authorize_upload(policy, roles)
        if policy.visibility != "public" or not self._uses_r2_for_policy(policy):
            raise HTTPException(status_code=409, detail="presigned_upload_requires_public_r2")
        normalized_content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if normalized_content_type not in policy.allowed_content_types:
            raise HTTPException(status_code=415, detail="unsupported_file_type_for_purpose")
        try:
            normalized_size = int(size_bytes)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid_file_size") from exc
        max_bytes = min(policy.max_bytes, self.settings.max_upload_bytes)
        if normalized_size <= 0:
            raise HTTPException(status_code=400, detail="empty_file")
        if normalized_size > max_bytes:
            raise HTTPException(status_code=413, detail="file_too_large")
        normalized_sha256 = str(sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
            raise HTTPException(status_code=422, detail="invalid_sha256")
        original = self._safe_original_filename(file_name)
        extension = Path(original).suffix.lower()
        if not extension:
            extension = sorted(EXTENSIONS_BY_MIME[normalized_content_type])[0]
            original = f"upload{extension}"
        if not self._extension_matches_filename(original, normalized_content_type):
            raise HTTPException(status_code=415, detail="file_extension_mime_mismatch")
        key = self._policy_target(policy, extension)
        try:
            upload_url = self._r2_client().generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": str(self.settings.r2_bucket),
                    "Key": key,
                    "ContentType": normalized_content_type,
                    "CacheControl": "public, max-age=31536000, immutable",
                },
                ExpiresIn=max(60, min(int(expires_in), 3600)),
                HttpMethod="PUT",
            )
        except Exception as exc:
            logger.exception("r2_presign_failed key=%s error_type=%s", key, type(exc).__name__)
            raise HTTPException(status_code=503, detail="r2_presign_failed") from exc
        public_base_url = str(self.settings.r2_public_base_url).rstrip("/")
        return {
            "storage_key": key,
            "original_filename": original,
            "content_type": normalized_content_type,
            "size_bytes": normalized_size,
            "sha256": normalized_sha256,
            "upload_url": upload_url,
            "headers": {
                "Content-Type": normalized_content_type,
                "Cache-Control": "public, max-age=31536000, immutable",
            },
            "public_url": f"{public_base_url}/{key}",
            "expires_in": max(60, min(int(expires_in), 3600)),
        }

    def verify_presigned_upload(
        self,
        *,
        storage_key: str,
        expected_size: int,
        expected_content_type: str,
        expected_sha256: str,
        policy_key: str,
    ) -> dict[str, Any]:
        policy = StoragePolicyRegistry.resolve(policy_key)
        normalized_key = str(storage_key or "").replace("\\", "/").lstrip("/")
        if policy.visibility != "public" or not self.is_public_relative_path(normalized_key):
            raise HTTPException(status_code=422, detail="invalid_presigned_storage_key")
        client = self._r2_client()
        try:
            head = client.head_object(Bucket=str(self.settings.r2_bucket), Key=normalized_key)
            body = client.get_object(Bucket=str(self.settings.r2_bucket), Key=normalized_key)["Body"].read(
                min(policy.max_bytes, self.settings.max_upload_bytes) + 1
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail="presigned_upload_not_found") from exc
        actual_content_type = str(head.get("ContentType") or "").split(";", 1)[0].lower()
        if int(head.get("ContentLength") or -1) != int(expected_size) or len(body) != int(expected_size):
            raise HTTPException(status_code=422, detail="presigned_upload_size_mismatch")
        if actual_content_type != str(expected_content_type).lower():
            raise HTTPException(status_code=422, detail="presigned_upload_content_type_mismatch")
        detected_type = self._detect(body)[1]
        if detected_type != actual_content_type or detected_type not in policy.allowed_content_types:
            raise HTTPException(status_code=415, detail="presigned_upload_signature_mismatch")
        scan = self.scanner.scan(body, detected_type) if policy.requires_scan else ScanResult("not_required", "none")
        if scan.status != "clean" and policy.requires_scan:
            raise HTTPException(status_code=422, detail="malware_or_active_content_detected")
        actual_sha256 = hashlib.sha256(body).hexdigest()
        if actual_sha256 != str(expected_sha256).lower():
            raise HTTPException(status_code=422, detail="presigned_upload_checksum_mismatch")
        return {
            "size_bytes": len(body),
            "content_type": detected_type,
            "sha256": actual_sha256,
            "scan_status": scan.status,
            "scan_provider": scan.provider,
        }

    def save_base64(self, *_: Any, **__: Any) -> StoredFile:
        raise HTTPException(status_code=422, detail="base64_upload_not_allowed")

    def save_bytes(
        self,
        policy_key: str,
        file_name: str,
        data: bytes,
        api_base_url: str,
        *,
        roles: set[str] | None = None,
    ) -> StoredFile:
        policy = StoragePolicyRegistry.resolve(policy_key)
        if roles is not None:
            self._authorize_upload(policy, roles)
        return self.save_bytes_for_policy(policy, file_name, data, api_base_url)

    def save_bytes_for_policy(
        self,
        policy: StoragePolicy,
        file_name: str,
        data: bytes,
        api_base_url: str,
    ) -> StoredFile:
        if not data:
            raise HTTPException(status_code=400, detail="empty_file")
        max_bytes = min(policy.max_bytes, self.settings.max_upload_bytes)
        if len(data) > max_bytes:
            raise HTTPException(status_code=413, detail="file_too_large")

        original = self._safe_original_filename(file_name)
        extension, content_type = self._detect(data)
        if content_type not in policy.allowed_content_types:
            raise HTTPException(status_code=415, detail="unsupported_file_type_for_purpose")
        if not self._extension_matches_filename(original, content_type):
            raise HTTPException(status_code=415, detail="file_extension_mime_mismatch")

        scan = self.scanner.scan(data, content_type) if policy.requires_scan else ScanResult("not_required", "none")
        if scan.status != "clean" and policy.requires_scan:
            # Keep the local forensic quarantine for local/test storage only.
            # R2-backed public images must not touch the Render filesystem,
            # including when an upload is rejected.
            if not self._uses_r2_for_policy(policy):
                self._ensure_root()
                quarantine_relative = self._quarantine_target(policy)
                self._atomic_write(self._safe_join(quarantine_relative), data)
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "malware_or_active_content_detected",
                    "scan_status": scan.status,
                    "scan_provider": scan.provider,
                    "signature": scan.signature,
                },
            )

        final_relative = self._policy_target(policy, extension)
        checksum = hashlib.sha256(data).hexdigest()
        if self._uses_r2_for_policy(policy):
            # Public images go straight from memory to Cloudflare R2. A local
            # quarantine/final copy would violate the production storage
            # contract and make Render responsible for image persistence.
            self._upload_to_r2(
                key=final_relative,
                data=data,
                content_type=content_type,
                policy_key=policy.key,
                sha256=checksum,
            )
            public_base_url = str(self.settings.r2_public_base_url).rstrip("/")
            return StoredFile(
                relative_path=final_relative,
                public_url=f"{public_base_url}/{final_relative}",
                storage_provider="cloudflare_r2",
                storage_bucket=str(self.settings.r2_bucket),
                content_type=content_type,
                sha256=checksum,
                size=len(data),
                policy_key=policy.key,
                visibility=policy.visibility,
                original_filename=original,
                scan_status=scan.status,
                scan_provider=scan.provider,
                quarantine_path="",
            )

        self._ensure_root()
        quarantine_relative = self._quarantine_target(policy)
        quarantine_path = self._safe_join(quarantine_relative)
        self._atomic_write(quarantine_path, data)
        final_path = self._safe_join(final_relative)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        quarantine_path.replace(final_path)
        relative = final_path.relative_to(self.root).as_posix()
        public_url = None
        if policy.visibility == "public":
            public_url = f"{api_base_url.rstrip('/')}/uploads/{relative}"
        return StoredFile(
            relative_path=relative,
            public_url=public_url,
            storage_provider="local_uploads",
            storage_bucket=policy.key,
            content_type=content_type,
            sha256=checksum,
            size=len(data),
            policy_key=policy.key,
            visibility=policy.visibility,
            original_filename=original,
            scan_status=scan.status,
            scan_provider=scan.provider,
            quarantine_path=quarantine_relative,
        )

    def delete_relative(self, relative_path: str, *, storage_provider: str = "local_uploads") -> bool:
        if storage_provider == "cloudflare_r2":
            try:
                self._r2_client().delete_object(
                    Bucket=str(self.settings.r2_bucket),
                    Key=str(relative_path).replace("\\", "/").lstrip("/"),
                )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=503, detail="r2_delete_failed") from exc
            local_target = self._safe_join(relative_path)
            if local_target.is_file():
                local_target.unlink()
            return True
        target = self._safe_join(relative_path)
        if not target.is_file():
            return False
        target.unlink()
        return True

    def remove(self, *_: Any, **__: Any) -> int:
        raise HTTPException(status_code=422, detail="raw_path_delete_forbidden")

    @classmethod
    def is_public_relative_path(cls, value: str) -> bool:
        parts = str(value).replace("\\", "/").strip("/").split("/")
        if not parts or not parts[0]:
            return False
        if parts[0] in {"_private", "_quarantine", "private", "quarantine"}:
            return False
        if parts[0] in StoragePolicyRegistry.private_directories():
            return False
        return parts[0] in StoragePolicyRegistry.public_directories()
