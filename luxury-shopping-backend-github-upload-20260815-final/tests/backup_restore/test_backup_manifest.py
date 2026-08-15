from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from backup_postgres import sha256_file, verify_backup_manifest


def test_manifest_checksum_detects_missing_or_changed_file(monkeypatch, tmp_path: Path):
    dump = tmp_path / "postgres.dump"
    dump.write_bytes(b"not a real pg dump")
    uploads = tmp_path / "uploads.zip"
    with zipfile.ZipFile(uploads, "w") as archive:
        archive.writestr("avatars/test.png", b"png")
    manifest = {
        "postgres_dump": {"path": str(dump), "name": dump.name, "size": dump.stat().st_size, "sha256": sha256_file(dump)},
        "uploads_archive": {"path": str(uploads), "name": uploads.name, "size": uploads.stat().st_size, "sha256": sha256_file(uploads)},
    }
    manifest_path = tmp_path / "backup_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def fake_restore_list(_dump_path):
        return object()

    monkeypatch.setattr("backup_postgres.run_pg_restore_list", fake_restore_list)
    ok_result = verify_backup_manifest(manifest_path)
    assert ok_result["ok"] is True

    dump.write_bytes(b"changed")
    changed_result = verify_backup_manifest(manifest_path)
    assert changed_result["ok"] is False
    assert any(item["name"] == "postgres_dump" and item["ok"] is False for item in changed_result["checks"])
