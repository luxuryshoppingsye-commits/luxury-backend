from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://unit:unit@127.0.0.1:5432/unit",
)
os.environ.setdefault("JWT_SECRET", "unit-test-secret-that-is-long-enough-123456789")

import backend.app.api.routes.operations as operations


@pytest.mark.asyncio
async def test_inventory_alerts_filter_forwards_warehouse_and_filters_movements(monkeypatch) -> None:
    seen: dict[str, str | None] = {}

    async def fake_inventory_payloads(session, warehouse_id=None):
        seen["warehouse_id"] = warehouse_id
        return [{"id": "inventory-2"}]

    async def fake_rows(session, table, limit=200):
        assert table == "inventory_movements"
        return [
            SimpleNamespace(id="movement-2", warehouse_id="warehouse-2"),
            SimpleNamespace(id="movement-1", warehouse_id="warehouse-1"),
        ]

    monkeypatch.setattr(operations, "_canonical_inventory_payloads", fake_inventory_payloads)
    monkeypatch.setattr(operations, "_rows", fake_rows)
    monkeypatch.setattr(
        operations,
        "serialize_record",
        lambda row: {"id": row.id, "warehouse_id": row.warehouse_id},
    )

    result = await operations.api_dashboard_inventory_alerts(
        warehouse_id="warehouse-2",
        staff=object(),
        session=object(),
    )

    assert seen["warehouse_id"] == "warehouse-2"
    assert result["data"]["inventory"] == [{"id": "inventory-2"}]
    assert result["data"]["movements"] == [
        {"id": "movement-2", "warehouse_id": "warehouse-2"},
    ]


@pytest.mark.asyncio
async def test_inventory_projection_excludes_products_missing_from_selected_warehouse(monkeypatch) -> None:
    product_rows = [
        SimpleNamespace(id="product-1"),
        SimpleNamespace(id="product-2"),
        SimpleNamespace(id="product-3"),
    ]
    products = {
        "product-1": {"id": "product-1", "stock_quantity": 100, "min_stock_quantity": 5},
        "product-2": {"id": "product-2", "stock_quantity": 100, "min_stock_quantity": 5},
        "product-3": {"id": "product-3", "stock_quantity": 100, "min_stock_quantity": 5},
    }
    locations = [
        {"id": "location-1", "product_id": "product-1", "warehouse_id": "warehouse-1", "quantity": 0},
        {"id": "location-2", "product_id": "product-2", "warehouse_id": "warehouse-2", "quantity": 0},
    ]

    async def fake_rows(session, table, limit=500):
        if table == "products":
            return product_rows
        if table == "inventory_locations":
            return locations
        return []

    async def fake_reference_maps(session):
        return products, {}

    monkeypatch.setattr(operations, "_rows", fake_rows)
    monkeypatch.setattr(operations, "_inventory_reference_maps", fake_reference_maps)
    monkeypatch.setattr(operations, "serialize_record", lambda row: row if isinstance(row, dict) else row.__dict__)
    monkeypatch.setattr(operations, "_enrich_inventory_payload", lambda payload, *args, **kwargs: payload)

    all_rows = await operations._canonical_inventory_payloads(object())
    warehouse_rows = await operations._canonical_inventory_payloads(object(), "warehouse-1")
    unassigned_rows = await operations._canonical_inventory_payloads(object(), "unassigned")

    assert {row["product_id"] for row in all_rows} == {"product-1", "product-2", "product-3"}
    assert {row["product_id"] for row in warehouse_rows} == {"product-1"}
    assert {row["product_id"] for row in unassigned_rows} == {
        "product-1",
        "product-2",
        "product-3",
    }
    assert {
        row["product_id"]: row["quantity"] for row in unassigned_rows
    } == {"product-1": 100, "product-2": 100, "product-3": 100}
    # A product can be healthy in aggregate while being empty in one selected
    # warehouse; the filter is scoped to that warehouse and must not rewrite
    # the global quantity to make the counts look monotonic.
    assert warehouse_rows[0]["quantity"] == 0
    assert all_rows[0]["quantity"] == 100


@pytest.mark.asyncio
async def test_inventory_projection_keeps_unassigned_remainder_visible(monkeypatch) -> None:
    product_rows = [SimpleNamespace(id="product-1")]
    products = {
        "product-1": {
            "id": "product-1",
            "stock_quantity": 100,
            "min_stock_quantity": 5,
        }
    }
    locations = [
        {
            "id": "location-1",
            "product_id": "product-1",
            "warehouse_id": "warehouse-1",
            "quantity": 40,
        }
    ]

    async def fake_rows(session, table, limit=500):
        if table == "products":
            return product_rows
        if table == "inventory_locations":
            return locations
        return []

    async def fake_reference_maps(session):
        return products, {"warehouse-1": {"id": "warehouse-1", "name": "الرئيسي"}}

    monkeypatch.setattr(operations, "_rows", fake_rows)
    monkeypatch.setattr(operations, "_inventory_reference_maps", fake_reference_maps)
    monkeypatch.setattr(
        operations,
        "serialize_record",
        lambda row: row if isinstance(row, dict) else row.__dict__,
    )

    all_rows = await operations._canonical_inventory_payloads(object())
    warehouse_rows = await operations._canonical_inventory_payloads(
        object(), "warehouse-1"
    )
    unassigned_rows = await operations._canonical_inventory_payloads(
        object(), "unassigned"
    )

    assert all_rows[0]["quantity"] == 100
    assert all_rows[0]["warehouse"]["name"] == "متعدد المستودعات / غير موزع"
    assert warehouse_rows[0]["quantity"] == 40
    assert unassigned_rows[0]["quantity"] == 60
    assert unassigned_rows[0]["warehouse"]["name"] == "غير موزع"


@pytest.mark.asyncio
async def test_warehouse_summary_counts_linked_products_and_unassigned_remainder(
    monkeypatch,
) -> None:
    warehouse_rows = [
        SimpleNamespace(id="warehouse-1"),
        SimpleNamespace(id="warehouse-2"),
    ]
    product_rows = [
        SimpleNamespace(id="product-1", stock_quantity=100),
        SimpleNamespace(id="product-2", stock_quantity=0),
    ]
    locations = [
        {
            "id": "location-1",
            "product_id": "product-1",
            "warehouse_id": "warehouse-1",
            "quantity": 40,
        },
        {
            "id": "location-2",
            "product_id": "product-2",
            "warehouse_id": "warehouse-1",
            "quantity": 0,
        },
    ]

    async def fake_rows(session, table, limit=500):
        if table == "warehouses":
            return warehouse_rows
        if table == "products":
            return product_rows
        if table == "inventory_locations":
            return locations
        return []

    monkeypatch.setattr(operations, "_rows", fake_rows)
    monkeypatch.setattr(
        operations,
        "serialize_record",
        lambda row: row if isinstance(row, dict) else row.__dict__,
    )

    result = await operations._warehouse_summary_payloads(object())

    assert result[0]["product_count"] == 2
    assert result[0]["total_quantity"] == 40
    assert result[0]["unassigned_product_count"] == 1
    assert result[0]["unassigned_quantity"] == 60
    assert result[1]["product_count"] == 0
    assert result[1]["total_quantity"] == 0
