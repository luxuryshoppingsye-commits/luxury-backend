from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.api.routes import operations
from backend.app.services import report_admin_services as support_services


@pytest.mark.asyncio
async def test_bulk_ticket_delete_marks_every_selected_ticket_and_audits(monkeypatch) -> None:
    actor = SimpleNamespace(id=uuid.uuid4())
    first = SimpleNamespace(id=uuid.uuid4(), deleted_at=None, extra_data={"workflow": []})
    second = SimpleNamespace(id=uuid.uuid4(), deleted_at=None, extra_data={"workflow": []})
    session = SimpleNamespace(commit=AsyncMock())
    audit_entries: list[dict] = []
    service = support_services.SupportWorkflowService()

    monkeypatch.setattr(service, "get", AsyncMock(side_effect=[first, second]))
    monkeypatch.setattr(
        support_services,
        "add_audit_log",
        lambda _session, **entry: audit_entries.append(entry),
    )

    result = await service.delete_many(
        session,
        ticket_ids=[first.id, second.id, first.id],
        user=actor,
        roles={"admin"},
    )

    assert result["ok"] is True
    assert result["deleted_count"] == 2
    assert set(result["ids"]) == {str(first.id), str(second.id)}
    assert first.deleted_at is not None and second.deleted_at is not None
    assert first.extra_data["workflow"][-1]["status"] == "deleted"
    assert second.extra_data["workflow"][-1]["by"] == str(actor.id)
    assert audit_entries[0]["action"] == "support_tickets.bulk_delete"
    assert audit_entries[0]["extra_data"]["record_ids"] == [str(first.id), str(second.id)]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_bulk_ticket_delete_route_passes_all_ids_to_service(monkeypatch) -> None:
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    delete_many = AsyncMock(return_value={"ok": True, "deleted_count": 2, "ids": [str(first_id), str(second_id)]})
    monkeypatch.setattr(operations.SupportWorkflowService, "delete_many", delete_many)

    class _Request:
        async def json(self):
            return {"ticket_ids": [str(first_id), str(second_id)]}

    result = await operations.api_delete_support_tickets_bulk(
        _Request(),
        staff=SimpleNamespace(id=uuid.uuid4()),
        roles={"admin"},
        session=object(),
    )

    assert result["deleted_count"] == 2
    assert delete_many.await_args.kwargs["ticket_ids"] == [first_id, second_id]
