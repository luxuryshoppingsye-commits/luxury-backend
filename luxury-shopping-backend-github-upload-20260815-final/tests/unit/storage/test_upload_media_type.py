from __future__ import annotations

from backend.app.main import _upload_media_type


def test_upload_media_type_uses_file_signature_over_extension(tmp_path):
    jpeg_named_webp = tmp_path / "product.webp"
    jpeg_named_webp.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)

    assert _upload_media_type(jpeg_named_webp) == "image/jpeg"


def test_upload_media_type_detects_real_webp(tmp_path):
    webp = tmp_path / "product.webp"
    webp.write_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 ")

    assert _upload_media_type(webp) == "image/webp"
