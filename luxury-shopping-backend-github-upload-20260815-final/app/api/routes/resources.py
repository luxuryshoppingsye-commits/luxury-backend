from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...dependencies import optional_user
from ...models.domain import User
from ...services.auth_service import roles_for
from ...repositories.resources import ResourceRepository, record_data_access


router = APIRouter(tags=["resources"])


@router.post("/resources/{table}/query")
async def query_resource(
    table: str,
    request: Request,
    user: User | None = Depends(optional_user),
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    roles = set(await roles_for(session, user.id)) if user else set()
    repository = ResourceRepository(session, table, user.id if user else None, roles)
    operation = str(body.get("operation") or "select")
    if operation == "select":
        result = await repository.select(body)
        if repository.is_staff and table != "data_access_logs" and user is not None:
            record_data_access(
                session,
                user_id=user.id,
                table=table,
                result=result,
                payload=body,
            )
            await session.commit()
        return result
    async with session.begin_nested():
        if operation == "insert":
            result = await repository.insert(body)
        elif operation == "upsert":
            result = await repository.insert(body, upsert=True)
        elif operation == "update":
            result = await repository.update(body)
        elif operation == "delete":
            result = await repository.delete(body)
        else:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="unsupported_resource_operation")
    await session.commit()
    return result
