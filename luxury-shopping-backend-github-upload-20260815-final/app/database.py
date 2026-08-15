from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings


settings = get_settings()
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_use_lifo=True,
    pool_recycle=900,
    pool_timeout=15,
)


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
