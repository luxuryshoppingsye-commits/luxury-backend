from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from backup_postgres import sha256_file, verify_backup_manifest


def test_corrupted_backup_manifest_fails_verification(monkeypatch, tmp_path: Path):
    dump = tmp_path / "postgres.dump"
    dump.write_bytes(b"valid-ish")
    uploads = tmp_path / "uploads.zip"
    with zipfile.ZipFile(uploads, "w") as archive:
        archive.writestr("support/file.txt", "ok")
    manifest = {
        "postgres_dump": {"path": str(dump), "name": dump.name, "size": dump.stat().st_size, "sha256": sha256_file(dump)},
        "uploads_archive": {"path": str(uploads), "name": uploads.name, "size": uploads.stat().st_size, "sha256": sha256_file(uploads)},
    }
    manifest_path = tmp_path / "backup_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def fake_restore_list(_dump_path):
        return object()

    monkeypatch.setattr("backup_postgres.run_pg_restore_list", fake_restore_list)
    assert verify_backup_manifest(manifest_path)["ok"] is True
    manifest["postgres_dump"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_backup_manifest(manifest_path)["ok"] is False
