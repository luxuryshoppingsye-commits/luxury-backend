from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..config import get_settings


T = TypeVar("T")


def cache_key(namespace: str, **parts: Any) -> str:
    normalized = "|".join(f"{name}={parts[name]!r}" for name in sorted(parts))
    return f"{namespace}|{normalized}"


class PublicReadCache:
    """Small per-process cache for anonymous public reads only.

    The short TTL keeps the storefront responsive while limiting staleness.
    Mutating requests clear the cache from the HTTP middleware, and private
    responses never enter this cache.
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int = 256) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self.max_entries = max(int(max_entries), 1)
        self._entries: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._generation = 0

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        if self.ttl_seconds <= 0:
            return await factory()

        cached = self._read(key)
        if cached is not None:
            return copy.deepcopy(cached)

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._read(key)
            if cached is not None:
                return copy.deepcopy(cached)
            generation = self._generation
            value = await factory()
            # A write can clear the cache while the database read is in
            # flight. Do not let that older read repopulate the cache after
            # invalidation has already happened.
            if generation == self._generation:
                self._entries[key] = (time.monotonic() + self.ttl_seconds, copy.deepcopy(value))
                self._trim()
            return value

    def clear(self) -> None:
        self._entries.clear()
        self._generation += 1

    def _read(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return value

    def _trim(self) -> None:
        if len(self._entries) <= self.max_entries:
            return
        oldest = sorted(self._entries.items(), key=lambda item: item[1][0])
        for key, _ in oldest[: len(self._entries) - self.max_entries]:
            self._entries.pop(key, None)


public_read_cache = PublicReadCache(
    ttl_seconds=get_settings().public_read_cache_ttl_seconds,
)
