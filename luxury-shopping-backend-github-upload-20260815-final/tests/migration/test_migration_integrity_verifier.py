from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "backend" / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from verify_migration_integrity import (  # noqa: E402
    SOURCE_KIND_LEGACY_STATE,
    SOURCE_KIND_NONE,
    SOURCE_KIND_SUPABASE_DB,
    SOURCE_KIND_SUPABASE_EXPORT,
    duplicate_source_keys,
    extract_upload_refs,
    load_legacy_state_source,
    source_kind,
)


def test_legacy_state_source_is_not_authoritative_supabase_export(tmp_path):
    user_id = str(uuid.uuid4())
    source = tmp_path / "state.json"
    source.write_text(json.dumps({"users": [{"id": user_id, "email": "user@example.com"}]}), encoding="utf-8")

    rows, metadata = load_legacy_state_source(source)

    assert metadata["ok"] is True
    assert metadata["source_kind"] == SOURCE_KIND_LEGACY_STATE
    assert metadata["is_authoritative_supabase_source"] is False
    assert rows["users"][0]["id"] == user_id


def test_source_kind_prefers_supabase_export_over_missing_source():
    args = argparse.Namespace(
        supabase_source_database_url=None,
        supabase_export_dir="C:/tmp/export",
        legacy_state_json=None,
    )

    assert source_kind(args) == SOURCE_KIND_SUPABASE_EXPORT


def test_source_kind_blocks_live_supabase_database_source(monkeypatch):
    monkeypatch.delenv("SUPABASE_SOURCE_DATABASE_URL", raising=False)
    args = argparse.Namespace(
        supabase_source_database_url="postgresql://example.supabase.co/postgres",
        supabase_export_dir=None,
        legacy_state_json=None,
    )

    assert source_kind(args) == SOURCE_KIND_SUPABASE_DB
    assert SOURCE_KIND_SUPABASE_DB == "forbidden_live_supabase_database"


def test_source_kind_reports_missing_when_no_source_is_supplied(monkeypatch):
    monkeypatch.delenv("SUPABASE_SOURCE_DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_EXPORT_DIR", raising=False)
    monkeypatch.delenv("LEGACY_STATE_JSON", raising=False)
    args = argparse.Namespace(
        supabase_source_database_url=None,
        supabase_export_dir=None,
        legacy_state_json=None,
    )

    assert source_kind(args) == SOURCE_KIND_NONE


def test_duplicate_source_keys_uses_primary_key_columns():
    rows = [{"id": "same"}, {"id": "same"}, {"id": "different"}]

    assert duplicate_source_keys(rows, ["id"]) == {"same": 2}


def test_extract_upload_refs_from_nested_payload():
    payload = {
        "image": "/uploads/products/a.png",
        "gallery": ["https://example.test/static.png", "/uploads/site-assets/banner.jpg"],
    }

    assert extract_upload_refs(payload) == {"/uploads/products/a.png", "/uploads/site-assets/banner.jpg"}
