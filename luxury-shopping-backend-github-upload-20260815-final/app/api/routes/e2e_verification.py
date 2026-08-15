from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...database import get_session
from ...models import MODEL_BY_TABLE, RESOURCE_TABLES
from ...repositories.resources import _column_value, serialize_record


router = APIRouter(prefix="/e2e", tags=["e2e-verification"])


def require_e2e_verification_enabled(settings: Settings) -> None:
    try:
        settings.require_test_fixtures_enabled("E2E verification")
    except RuntimeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _coerce_filter(model: Any, item: dict[str, Any]):
    table = model.__table__
    column_name = str(item.get("column") or "").strip()
    operator = str(item.get("operator") or "eq").strip().lower()
    if not column_name or column_name not in table.c:
        raise HTTPException(status_code=400, detail=f"unknown_filter_column:{column_name}")
    column = table.c[column_name]
    if operator == "eq":
        value = _column_value(column, item.get("value"))
        return column == value
    if operator == "neq":
        value = _column_value(column, item.get("value"))
        return column != value
    if operator == "is_null":
        return column.is_(None)
    if operator == "is_not_null":
        return column.is_not(None)
    raise HTTPException(status_code=400, detail=f"unsupported_filter_operator:{operator}")


def _project_row(row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    if not fields:
        return row
    return {field: row.get(field) for field in fields if field in row}


@router.post("/verify/{table}")
async def verify_table_rows(
    table: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    require_e2e_verification_enabled(settings)
    if table not in RESOURCE_TABLES:
        raise HTTPException(status_code=404, detail="resource_not_found")

    body = await request.json()
    filters = body.get("filters") or []
    fields = body.get("fields") or []
    limit = int(body.get("limit") or 20)
    include_deleted = bool(body.get("include_deleted"))
    if not isinstance(filters, list):
        raise HTTPException(status_code=400, detail="filters_must_be_list")
    if not isinstance(fields, list):
        raise HTTPException(status_code=400, detail="fields_must_be_list")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="invalid_limit")

    model = MODEL_BY_TABLE[table]
    clauses = [_coerce_filter(model, item) for item in filters]
    if not include_deleted and "deleted_at" in model.__table__.c:
        clauses.append(model.__table__.c.deleted_at.is_(None))
    statement = select(model).limit(limit)
    if clauses:
        statement = statement.where(and_(*clauses))
    result = await session.execute(statement)
    rows = [
        _project_row(serialize_record(record), [str(field) for field in fields])
        for record in result.scalars().all()
    ]
    return {
        "ok": True,
        "table": table,
        "count": len(rows),
        "rows": rows,
        "database_name": settings.database_name,
        "app_env": settings.app_env,
    }
