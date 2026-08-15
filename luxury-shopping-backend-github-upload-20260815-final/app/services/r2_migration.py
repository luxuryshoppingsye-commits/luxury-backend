from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import OFFICIAL_RENDER_API_ORIGIN, get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import FileAsset
from ..storage import FileStorage


REFERENCE_FIELDS = frozenset(
    {
        "avatar_url",
        "banner_url",
        "cover_url",
        "image_url",
        "images",
        "logo_url",
        "product_image",
        "store_logo_url",
        "thumbnail_url",
    }
)
LEGACY_REFERENCE_TABLES = frozenset(
    {
        "profiles",
        "categories",
        "brands",
        "products",
        "product_variants",
        "banners",
        "banner_history",
        "local_merchants",
        "partner_storefronts",
        "partner_applications",
        "partner_profiles",
        "global_sites",
        "blog_articles",
    }
)
DEFAULT_LEGACY_REFERENCE_TABLES = frozenset({"products", "product_variants"})
LEGACY_DIRECTORY_ALIASES = {
    "product-images": "products",
    "product-variant-images": "product-variants",
}


def _replace_exact(value: Any, replacements: dict[str, str]) -> tuple[Any, int]:
    if isinstance(value, str):
        replacement = replacements.get(value)
        return (replacement, 1) if replacement is not None else (value, 0)
    if isinstance(value, list):
        changed = 0
        result = []
        for item in value:
            new_item, item_changed = _replace_exact(item, replacements)
            result.append(new_item)
            changed += item_changed
        return result, changed
    if isinstance(value, dict):
        changed = 0
        result: dict[Any, Any] = {}
        for key, item in value.items():
            new_item, item_changed = _replace_exact(item, replacements)
            result[key] = new_item
            changed += item_changed
        return result, changed
    return value, 0


def _replacement_map(storage_key: str, public_base_url: str) -> dict[str, str]:
    key = str(storage_key).replace("\\", "/").lstrip("/")
    public_url = f"{public_base_url.rstrip('/')}/{key}"
    legacy_keys = {key}
    for legacy_directory, canonical_directory in LEGACY_DIRECTORY_ALIASES.items():
        prefix = f"{canonical_directory}/"
        if key.startswith(prefix):
            legacy_keys.add(f"{legacy_directory}/{key[len(prefix):]}")
    old_values = {
        value
        for legacy_key in legacy_keys
        for value in (
            legacy_key,
            f"/uploads/{legacy_key}",
            f"/api/uploads/{legacy_key}",
            f"{OFFICIAL_RENDER_API_ORIGIN}/uploads/{legacy_key}",
            f"{OFFICIAL_RENDER_API_ORIGIN}/api/uploads/{legacy_key}",
        )
    }
    settings = get_settings()
    for base in ("http://testserver", settings.api_base_url, settings.app_public_url):
        normalized = str(base or "").rstrip("/")
        if normalized:
            for legacy_key in legacy_keys:
                old_values.update(
                    {f"{normalized}/uploads/{legacy_key}", f"{normalized}/api/uploads/{legacy_key}"}
                )
    return {value: public_url for value in old_values}


class R2MigrationService:
    """Move public Render-local assets without deleting the Render copy."""

    def __init__(self, storage: FileStorage | None = None) -> None:
        self.storage = storage or FileStorage()
        self.settings = get_settings()

    async def migrate(
        self,
        session: AsyncSession,
        *,
        apply: bool,
        retention_days: int = 7,
        limit: int = 500,
        actor_id: Any | None = None,
        reference_tables: set[str] | None = None,
    ) -> dict[str, Any]:
        if str(self.settings.storage_provider).lower() != "r2":
            raise ValueError("r2_storage_required")
        retention_days = max(1, min(int(retention_days), 30))
        limit = max(1, min(int(limit), 2000))
        scan_tables = (
            DEFAULT_LEGACY_REFERENCE_TABLES
            if reference_tables is None
            else frozenset(reference_tables) & LEGACY_REFERENCE_TABLES
        )
        now = datetime.now(timezone.utc)
        report: dict[str, Any] = {
            "apply": apply,
            "retention_days": retention_days,
            "render_retention_until": (now + timedelta(days=retention_days)).isoformat(),
            "scanned_assets": 0,
            "candidate_assets": 0,
            "migrated_assets": 0,
            "legacy_reference_candidates": 0,
            "legacy_file_candidates": 0,
            "migrated_legacy_files": 0,
            "legacy_scan_tables": sorted(scan_tables),
            "already_r2": 0,
            "skipped_invalid_path": 0,
            "skipped_missing_local_file": 0,
            "failed_assets": [],
            "updated_references": 0,
            "updated_tables": {},
        }

        result = await session.execute(
            select(FileAsset)
            .where(
                FileAsset.deleted_at.is_(None),
                FileAsset.visibility == "public",
                FileAsset.storage_provider != "cloudflare_r2",
                FileAsset.status != "deleted",
            )
            .order_by(FileAsset.created_at.asc())
            .limit(limit)
        )
        assets = list(result.scalars())
        existing_result = await session.execute(
            select(FileAsset).where(FileAsset.deleted_at.is_(None))
        )
        existing_assets = list(existing_result.scalars())
        assets_by_key = {
            str(asset.storage_key).replace("\\", "/").lstrip("/"): asset
            for asset in existing_assets
            if asset.storage_key
        }
        replacements: dict[str, str] = {}
        migrated_keys: set[str] = set()

        for asset in assets:
            report["scanned_assets"] += 1
            storage_key = str(asset.storage_key or "").replace("\\", "/").lstrip("/")
            if not self.storage.is_public_relative_path(storage_key):
                report["skipped_invalid_path"] += 1
                continue
            local_path = self.storage._safe_join(storage_key)
            if not local_path.is_file():
                report["skipped_missing_local_file"] += 1
                continue
            report["candidate_assets"] += 1
            public_base = str(self.settings.r2_public_base_url).rstrip("/")
            replacements.update(_replacement_map(storage_key, public_base))
            if not apply:
                continue
            try:
                data = await asyncio.to_thread(local_path.read_bytes)
                detected_content_type = self.storage._detect(data)[1]
                if detected_content_type != str(asset.content_type).lower():
                    raise ValueError("legacy_content_type_mismatch")
                checksum = hashlib.sha256(data).hexdigest()
                await asyncio.to_thread(
                    self._copy_and_verify,
                    storage_key,
                    data,
                    detected_content_type,
                    checksum,
                    str(asset.policy_key),
                )
                asset.storage_provider = "cloudflare_r2"
                asset.storage_bucket = str(self.settings.r2_bucket)
                extra_data = dict(asset.extra_data or {})
                extra_data["render_to_r2_migration"] = {
                    "migrated_at": now.isoformat(),
                    "source": "render_local_upload",
                    "render_retention_days": retention_days,
                    "render_copy_kept": True,
                    "sha256_verified": True,
                }
                asset.extra_data = extra_data
                report["migrated_assets"] += 1
                migrated_keys.add(storage_key)
            except Exception as exc:  # one bad legacy file must not stop the batch
                report["failed_assets"].append(
                    {
                        "file_id": str(asset.id),
                        "storage_key": storage_key,
                        "error": type(exc).__name__,
                    }
                )

        # Older seeded catalog rows predate file_assets and contain direct
        # /uploads/... references. They still need the same verified transfer.
        legacy_keys = await self._collect_legacy_keys(session, scan_tables)
        report["legacy_reference_candidates"] = len(legacy_keys)
        remaining = max(0, limit - len(migrated_keys))
        for storage_key in legacy_keys[:remaining]:
            if storage_key in migrated_keys:
                continue
            local_path = self.storage._safe_join(storage_key)
            asset = assets_by_key.get(storage_key)
            if not local_path.is_file():
                if asset is not None and asset.storage_provider == "cloudflare_r2":
                    public_base = str(self.settings.r2_public_base_url).rstrip("/")
                    replacements.update(_replacement_map(storage_key, public_base))
                    report["already_r2"] += 1
                    migrated_keys.add(storage_key)
                    continue
                report["skipped_missing_local_file"] += 1
                continue
            policy_key = self._policy_key_for_storage_key(storage_key)
            if policy_key is None:
                report["skipped_invalid_path"] += 1
                continue
            try:
                data = await asyncio.to_thread(local_path.read_bytes)
                detected_content_type = self.storage._detect(data)[1]
                policy = self.storage_policy(policy_key)
                if len(data) > min(policy.max_bytes, self.settings.max_upload_bytes):
                    raise ValueError("legacy_file_too_large")
                if detected_content_type not in policy.allowed_content_types:
                    raise ValueError("legacy_content_type_not_allowed")
                checksum = hashlib.sha256(data).hexdigest()
                report["legacy_file_candidates"] += 1
                if not apply:
                    continue
                await asyncio.to_thread(
                    self._copy_and_verify,
                    storage_key,
                    data,
                    detected_content_type,
                    checksum,
                    policy_key,
                )
                migration_metadata = {
                    "render_to_r2_migration": {
                        "migrated_at": now.isoformat(),
                        "source": "legacy_catalog_reference",
                        "render_retention_days": retention_days,
                        "render_copy_kept": True,
                        "sha256_verified": True,
                    }
                }
                if asset is None:
                    asset = FileAsset(
                        owner_user_id=None,
                        created_by=actor_id,
                        policy_key=policy_key,
                        visibility="public",
                        storage_provider="cloudflare_r2",
                        storage_bucket=str(self.settings.r2_bucket),
                        storage_key=storage_key,
                        original_filename=storage_key.rsplit("/", 1)[-1],
                        content_type=detected_content_type,
                        size_bytes=len(data),
                        checksum_sha256=checksum,
                        status="available",
                        scan_status="clean",
                        scan_provider="render-to-r2-migration",
                        extra_data=migration_metadata,
                    )
                    session.add(asset)
                    assets_by_key[storage_key] = asset
                else:
                    asset.storage_provider = "cloudflare_r2"
                    asset.storage_bucket = str(self.settings.r2_bucket)
                    asset.content_type = detected_content_type
                    asset.size_bytes = len(data)
                    asset.checksum_sha256 = checksum
                    asset.extra_data = {**dict(asset.extra_data or {}), **migration_metadata}
                public_base = str(self.settings.r2_public_base_url).rstrip("/")
                replacements.update(_replacement_map(storage_key, public_base))
                migrated_keys.add(storage_key)
                report["migrated_legacy_files"] += 1
            except Exception as exc:
                report["failed_assets"].append(
                    {
                        "storage_key": storage_key,
                        "error": type(exc).__name__,
                    }
                )

        if apply and replacements:
            reference_report = await self._update_references(session, replacements, scan_tables)
            report["updated_references"] = reference_report["updated_references"]
            report["updated_tables"] = reference_report["updated_tables"]
            await session.commit()
        return report

    @staticmethod
    def _policy_key_for_storage_key(storage_key: str) -> str | None:
        directory = str(storage_key).split("/", 1)[0]
        return {
            "avatars": "avatar",
            "products": "product_image",
            "product-variants": "product_variant_image",
            "site-assets": "site_asset",
            "merchant-assets": "merchant_asset",
        }.get(directory)

    @staticmethod
    def storage_policy(policy_key: str):
        from ..storage import StoragePolicyRegistry

        return StoragePolicyRegistry.resolve(policy_key)

    @staticmethod
    def _legacy_key_from_reference(value: Any) -> str | None:
        if not isinstance(value, str) or "/uploads/" not in value:
            return None
        candidate = value.strip()
        if "://" in candidate:
            candidate = urlsplit(candidate).path
        candidate = candidate.split("?", 1)[0].split("#", 1)[0]
        marker = "/uploads/"
        suffix = candidate.split(marker, 1)[1].lstrip("/")
        directory, separator, remainder = suffix.partition("/")
        directory = LEGACY_DIRECTORY_ALIASES.get(directory, directory)
        suffix = f"{directory}/{remainder}" if separator else directory
        return suffix if FileStorage.is_public_relative_path(suffix) else None

    async def _collect_legacy_keys(
        self,
        session: AsyncSession,
        scan_tables: frozenset[str],
    ) -> list[str]:
        keys: set[str] = set()
        for table_name, model in MODEL_BY_TABLE.items():
            if table_name not in scan_tables:
                continue
            fields = self._reference_fields_for_model(table_name, model, scan_tables)
            if not fields:
                continue
            columns = [getattr(model, field) for field in fields]
            result = await session.execute(select(*columns))
            for row in result:
                for value in row:
                    self._collect_from_value(value, keys)
        return sorted(keys)

    @staticmethod
    def _reference_fields_for_model(
        table_name: str,
        model: Any,
        scan_tables: frozenset[str],
    ) -> list[str]:
        fields = [field for field in REFERENCE_FIELDS if field in model.__table__.c]
        if table_name in scan_tables and "extra_data" in model.__table__.c:
            fields.append("extra_data")
        return fields

    def _collect_from_value(self, value: Any, keys: set[str]) -> None:
        if isinstance(value, str):
            key = self._legacy_key_from_reference(value)
            if key:
                keys.add(key)
            return
        if isinstance(value, list):
            for item in value:
                self._collect_from_value(item, keys)
            return
        if isinstance(value, dict):
            for item in value.values():
                self._collect_from_value(item, keys)

    def _copy_and_verify(
        self,
        storage_key: str,
        data: bytes,
        content_type: str,
        checksum: str,
        policy_key: str,
    ) -> None:
        client = self.storage._r2_client()
        bucket = str(self.settings.r2_bucket)
        cache_control = "public, max-age=31536000, immutable"
        head = None
        try:
            head = client.head_object(Bucket=bucket, Key=storage_key)
        except Exception:
            head = None
        metadata = {str(k).lower(): str(v) for k, v in (head or {}).get("Metadata", {}).items()}
        if (
            not head
            or int(head.get("ContentLength") or -1) != len(data)
            or metadata.get("sha256") != checksum
            or str(head.get("CacheControl") or "") != cache_control
        ):
            client.put_object(
                Bucket=bucket,
                Key=storage_key,
                Body=data,
                ContentType=content_type,
                CacheControl=cache_control,
                Metadata={"sha256": checksum, "policy": policy_key, "migrated-from": "render"},
            )
        verified = client.get_object(Bucket=bucket, Key=storage_key)
        body = verified["Body"].read()
        if len(body) != len(data) or hashlib.sha256(body).hexdigest() != checksum:
            raise RuntimeError("r2_checksum_verification_failed")

    async def _update_references(
        self,
        session: AsyncSession,
        replacements: dict[str, str],
        scan_tables: frozenset[str],
    ) -> dict[str, Any]:
        updated = 0
        tables: dict[str, int] = {}
        for table_name, model in MODEL_BY_TABLE.items():
            if table_name == "file_assets":
                continue
            fields = self._reference_fields_for_model(table_name, model, scan_tables)
            if not fields:
                continue
            result = await session.execute(select(model))
            for row in result.scalars():
                row_changed = 0
                for field in fields:
                    value = getattr(row, field, None)
                    new_value, changed = _replace_exact(value, replacements)
                    if changed:
                        setattr(row, field, new_value)
                        row_changed += changed
                if row_changed:
                    updated += row_changed
                    tables[table_name] = tables.get(table_name, 0) + row_changed
        return {"updated_references": updated, "updated_tables": tables}
