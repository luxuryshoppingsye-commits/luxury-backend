from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from backup_postgres import compare_upload_manifests, create_uploads_archive, extract_uploads_archive, upload_manifest


def test_upload_archive_roundtrip_preserves_manifest(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    (upload_dir / "products").mkdir(parents=True)
    (upload_dir / "products" / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    before = upload_manifest(upload_dir)
    archive = tmp_path / "uploads.zip"
    create_uploads_archive(upload_dir, archive)
    restore_dir = tmp_path / "restored"
    extract_uploads_archive(archive, restore_dir)
    after = upload_manifest(restore_dir)
    assert compare_upload_manifests(before, after)["ok"] is True


def test_upload_archive_rejects_path_traversal(tmp_path: Path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../evil.txt", "bad")
    with pytest.raises(RuntimeError, match="Unsafe upload archive path"):
        extract_uploads_archive(archive, tmp_path / "restore")
