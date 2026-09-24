"""LangGraph's PostgreSQL checkpointer.

Checkpoints are the graph's own recovery log. They live in tables LangGraph
owns and migrates itself, which is why nothing here is an Alembic migration and
why no repository reads these tables: application history lives in
``messages``, and mixing the two would make each one's guarantees depend on the
other's schema.

The checkpointer speaks psycopg, while the application's engine speaks asyncpg.
Rather than force one driver on both, the DSN is translated here — the two pools
are independent by design, since a stalled checkpoint write must not exhaust the
connections the API needs to answer requests.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import Settings

logger = logging.getLogger(__name__)


def psycopg_dsn(database_url: str) -> str:
    """Translate the application's SQLAlchemy URL into a plain psycopg DSN."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def checkpointer_for(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """Open a checkpointer for the lifetime of the application.

    ``setup()`` creates or migrates LangGraph's tables. It is idempotent, and
    running it at startup keeps a single dev replica working out of the box;
    production runs it from the same release job as the Alembic migrations so
    that replicas never race each other (AGENTS.md §14).
    """
    dsn = psycopg_dsn(str(settings.database_url))
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
        logger.info("checkpointer_ready")
        yield saver
