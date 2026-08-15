from __future__ import annotations

import zipfile

from backend.app.main import _extract_seed_uploads


def test_seed_upload_bootstrap_extracts_missing_files(monkeypatch, tmp_path):
    seed_zip = tmp_path / "uploads_seed.zip"
    with zipfile.ZipFile(seed_zip, "w") as archive:
        archive.writestr("products/product.webp", b"RIFF1234WEBPdata")
        archive.writestr("../outside.webp", b"bad")

    monkeypatch.setattr("backend.app.main.SEED_UPLOADS_ZIP", seed_zip)

    root = tmp_path / "uploads"
    result = _extract_seed_uploads(root)

    assert result == 1
    assert (root / "products" / "product.webp").read_bytes() == b"RIFF1234WEBPdata"
    assert not (tmp_path / "outside.webp").exists()


def test_seed_upload_bootstrap_does_not_overwrite_existing_files(monkeypatch, tmp_path):
    seed_zip = tmp_path / "uploads_seed.zip"
    with zipfile.ZipFile(seed_zip, "w") as archive:
        archive.writestr("products/product.webp", b"new")

    root = tmp_path / "uploads"
    existing = root / "products" / "product.webp"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")

    monkeypatch.setattr("backend.app.main.SEED_UPLOADS_ZIP", seed_zip)

    result = _extract_seed_uploads(root)

    assert result == 0
    assert existing.read_bytes() == b"old"
