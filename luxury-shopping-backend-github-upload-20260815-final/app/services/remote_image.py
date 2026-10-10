from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import HTTPException


def validate_image_url(value: str) -> httpx.URL:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise HTTPException(422, "invalid_image_url")
    try:
        url = httpx.URL(value.strip())
        parsed = urlsplit(str(url))
        if url.scheme != "https" or not url.host or parsed.username or parsed.password or url.port not in (None, 443):
            raise ValueError()
        if "%" in url.host or url.host.lower().rstrip(".") in {"localhost", "metadata.google.internal"}:
            raise ValueError()
    except (ValueError, httpx.InvalidURL):
        raise HTTPException(422, "invalid_image_url") from None
    return url.copy_with(fragment=None)


async def public_image_address(host: str) -> str:
    try:
        answers = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout=5
        )
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses:
            raise ValueError()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            mapped = getattr(ip, "ipv4_mapped", None)
            special = ("192.0.0.0/24", "192.88.99.0/24") if ip.version == 4 else ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "fec0::/10")
            if not ip.is_global or ip.is_multicast or ip.is_reserved or any(ip in ipaddress.ip_network(network) for network in special) or (mapped and not mapped.is_global):
                raise HTTPException(422, "private_image_address_forbidden")
        return sorted(addresses, key=lambda value: ":" in value)[0]
    except (ValueError, OSError, asyncio.TimeoutError):
        raise HTTPException(422, "image_host_unavailable") from None


async def download_product_image(value: str, max_bytes: int) -> tuple[bytes, str]:
    # Connect to the validated address directly while retaining TLS hostname
    # verification. A later DNS response cannot redirect this request locally.
    url = validate_image_url(value)
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                for _ in range(4):
                    address = await public_image_address(url.host)
                    pinned = url.copy_with(host=address)
                    async with client.stream(
                        "GET", pinned,
                        headers={"Host": url.host, "Accept": "image/jpeg,image/png,image/webp,image/gif", "Accept-Encoding": "identity"},
                        extensions={"sni_hostname": url.host},
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise HTTPException(422, "invalid_image_redirect")
                            url = validate_image_url(urljoin(str(url), location))
                            continue
                        if response.status_code != 200:
                            raise HTTPException(422, "image_download_failed")
                        mime = response.headers.get("content-type", "").split(";")[0].lower().strip()
                        if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                            raise HTTPException(415, "unsupported_image_type")
                        size = response.headers.get("content-length")
                        if size and (not size.isdigit() or int(size) > max_bytes):
                            raise HTTPException(413, "image_too_large")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > max_bytes:
                                raise HTTPException(413, "image_too_large")
                        if not data:
                            raise HTTPException(422, "empty_image")
                        return bytes(data), mime
                raise HTTPException(422, "too_many_image_redirects")
    except (httpx.HTTPError, asyncio.TimeoutError):
        raise HTTPException(422, "image_download_failed") from None
