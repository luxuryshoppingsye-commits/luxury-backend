from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from .config import get_settings


settings = get_settings()
engine_options = {"pool_pre_ping": True}
if settings.is_test_environment:
    # Tests create and tear down event loops per module. A real pool can retain
    # asyncpg connections bound to an earlier loop and make later test groups
    # fail with "Future attached to a different loop". A NullPool keeps every
    # test database connection scoped to the loop that opened it.
    engine_options["poolclass"] = NullPool
else:
    engine_options.update(
        pool_size=10,
        max_overflow=20,
        pool_use_lifo=True,
        pool_recycle=900,
        pool_timeout=15,
    )
engine = create_async_engine(settings.database_url, **engine_options)


@event.listens_for(engine.sync_engine, "connect")
def set_public_search_path(dbapi_connection, _: object) -> None:
    dbapi_connection.run_async(
        lambda connection: connection.execute("SET search_path TO public")
    )


SessionFactory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


async def database_ready() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False
