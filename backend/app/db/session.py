"""Async SQLAlchemy engine and session lifecycle.

The engine owns a connection pool, so exactly one is built per process during
the FastAPI lifespan and disposed on shutdown. Nothing here is constructed at
import time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


class Database:
    """Owns the async engine and hands out sessions."""

    def __init__(self, settings: Settings) -> None:
        self._engine: AsyncEngine = create_async_engine(
            str(settings.database_url),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_pre_ping=True,
            echo=False,
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, rolling back on error and always closing."""
        async with self._sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    def open_session(self) -> AsyncSession:
        """A session for work outside a request, used as ``async with``.

        Background run execution has no request-scoped dependency to hand it a
        session; the context manager closes it deterministically, which breaking
        out of ``session()``'s generator would not.
        """
        return self._sessionmaker()

    async def check_connection(self) -> bool:
        """Cheap round-trip used by the readiness probe."""
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()
