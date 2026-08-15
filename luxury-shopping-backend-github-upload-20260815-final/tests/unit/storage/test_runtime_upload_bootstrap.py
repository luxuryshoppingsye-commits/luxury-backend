from __future__ import annotations

from backend.app.main import _extract_seed_uploads, _mirror_legacy_product_image_paths


def test_seed_uploads_extract_and_mirror_product_images(tmp_path):
    _extract_seed_uploads(tmp_path)
    _mirror_legacy_product_image_paths(tmp_path)

    product_image = tmp_path / "product-images" / "da509cea-406f-43aa-9962-ad29a5ed738e-3.webp"
    public_alias = tmp_path / "products" / "da509cea-406f-43aa-9962-ad29a5ed738e-3.webp"
    second_alias = tmp_path / "products" / "b9ceb393-29d2-4869-90a4-d94502aba7b0-1.webp"

    assert product_image.is_file()
    assert public_alias.is_file()
    assert second_alias.is_file()
    assert public_alias.read_bytes()[:4] == b"RIFF"
    assert public_alias.read_bytes()[8:12] == b"WEBP"
    assert second_alias.read_bytes()[:4] == b"RIFF"
    assert second_alias.read_bytes()[8:12] == b"WEBP"
