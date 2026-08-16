import asyncio

import pytest

from app.services.public_read_cache import PublicReadCache


@pytest.mark.asyncio
async def test_public_read_cache_reuses_value_until_ttl_expires():
    cache = PublicReadCache(ttl_seconds=60)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return {"items": [calls]}

    first = await cache.get_or_set("catalog", factory)
    first["items"].append(99)
    second = await cache.get_or_set("catalog", factory)

    assert calls == 1
    assert second == {"items": [1]}


@pytest.mark.asyncio
async def test_public_read_cache_collapses_concurrent_misses_and_clear_forces_refresh():
    cache = PublicReadCache(ttl_seconds=60)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"value": calls}

    values = await asyncio.gather(*(cache.get_or_set("same", factory) for _ in range(8)))
    assert calls == 1
    assert values == [{"value": 1}] * 8

    cache.clear()
    assert await cache.get_or_set("same", factory) == {"value": 2}
    assert calls == 2


@pytest.mark.asyncio
async def test_clear_during_inflight_read_does_not_reinsert_stale_value():
    cache = PublicReadCache(ttl_seconds=60)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"value": calls}

    pending = asyncio.create_task(cache.get_or_set("same", factory))
    await started.wait()
    cache.clear()
    release.set()
    assert await pending == {"value": 1}

    assert await cache.get_or_set("same", factory) == {"value": 2}
    assert calls == 2
