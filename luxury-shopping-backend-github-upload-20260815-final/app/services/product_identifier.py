import base64
import binascii
import re
import uuid


COMPACT_UUID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")


def decode_compact_uuid(identifier: str) -> uuid.UUID | None:
    """Decode the 22-character URL-safe representation of a UUID."""
    value = str(identifier or "").strip()
    if not COMPACT_UUID_PATTERN.fullmatch(value):
        return None
    try:
        padded = value.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 16:
        return None
    return uuid.UUID(bytes=raw)
