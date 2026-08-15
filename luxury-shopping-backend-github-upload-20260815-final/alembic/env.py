from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_migration_url or settings.database_url)
target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to):
    # The old JSONB migration source is retained for audit only. Alembic does
    # not own it and must never suggest dropping it.
    if name == "luxury_collections":
        return False
    if type_ == "index" and reflected and compare_to is None and name and (
        name.startswith("ix_products_search_") or name.startswith("ix_products_public_")
        or name.startswith("ix_financial_") or name.startswith("uq_refunds_idempotency_")
    ):
        return False
    if type_ == "check_constraint" and reflected and compare_to is None and name and name.startswith("ck_financial_"):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
