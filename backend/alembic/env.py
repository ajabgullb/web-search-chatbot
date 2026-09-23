"""Alembic environment.

The database URL comes from the same typed settings the application uses, so
migrations can never drift onto a different database than the API. Autogenerate
targets ``Base.metadata``; every model module must be imported below to be seen.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Imported for the side effect of registering every model on Base.metadata,
# which is what autogenerate diffs against.
import app.models  # noqa: F401  (side-effect import)
from alembic import context
from app.core.config import get_settings
from app.db.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", str(get_settings().database_url))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# LangGraph's checkpointer creates and migrates these tables itself. They are
# not in Base.metadata, so without this filter autogenerate reads them as
# "removed" and writes a migration that drops them.
_EXTERNALLY_OWNED_TABLES = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)


def _include_name(name: str | None, type_: str, parent_names: object) -> bool:
    return not (type_ == "table" and name in _EXTERNALLY_OWNED_TABLES)


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type and server-default changes, not just added/dropped
        # tables, so autogenerate diffs stay trustworthy.
        compare_type=True,
        compare_server_default=True,
        include_name=_include_name,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
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
