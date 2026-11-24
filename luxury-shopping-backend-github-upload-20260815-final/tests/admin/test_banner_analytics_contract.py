import asyncio
import uuid
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionFactory
from app.main import app
from app.models import MODEL_BY_TABLE
from app.services.public_read_cache import public_read_cache


@pytest.mark.asyncio
async def test_guest_banner_events_update_counts_without_double_counting(monkeypatch):
    settings = get_settings()
    assert settings.is_test_environment
    assert urlsplit(settings.database_url).hostname == "127.0.0.1"
    monkeypatch.setattr(public_read_cache, "ttl_seconds", 0)

    banners = MODEL_BY_TABLE["banners"]
    events = MODEL_BY_TABLE["analytics_events"]
    banner_id = uuid.uuid4()
    async with SessionFactory() as session:
        session.add(banners(
            id=banner_id, title="عطور فاخرة", image_url="/banner.webp", is_active=True, status="active",
            extra_data={"view_count": 4, "click_count": 2, "position": "home"},
        ))
        await session.commit()

    endpoint = f"/api/catalog/banners/{banner_id}/events"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8106",
    ) as client:
        observed = {"type": "view", "eventKey": "observed-visit"}
        views = await asyncio.gather(*(client.post(endpoint, json=observed) for _ in range(3)))
        assert all(response.status_code == 200 and response.json()["tracked"] for response in views)
        click = await client.post(endpoint, json={"type": "click", "eventKey": "observed-visit"})
        assert click.status_code == 200 and click.json()["tracked"]
        unsupported = await client.post(endpoint, json={"type": "purchase"})
        assert unsupported.status_code == 422
        invalid = await client.post(endpoint, content="{", headers={"Content-Type": "application/json"})
        assert invalid.status_code == 400
        unknown = await client.post(f"/api/catalog/banners/{uuid.uuid4()}/events", json=observed)
        assert unknown.status_code == 404
        admin_write = await client.post("/api/catalog/admin/banners", json={"title": "عطر"})
        assert admin_write.status_code == 401

        listing = await client.get("/api/catalog/banners?position=home")
        assert listing.status_code == 200
        banner = next(item for item in listing.json()["data"] if item["id"] == str(banner_id))
        assert (banner["view_count"], banner["click_count"], banner["position"]) == (5, 3, "home")

    async with SessionFactory() as session:
        stored = (
            await session.execute(select(events.type).where(events.description == str(banner_id)))
        ).scalars()
        assert sorted(stored) == ["banner_click", "banner_view"]
