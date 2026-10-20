"""The migration refuses a data volume reattached across C libraries.

Moving from ``postgres:16-alpine`` (musl) to ``pgvector/pgvector`` (glibc) by
reattaching the old volume leaves text indexes sorted under the wrong rules,
and PostgreSQL does not warn. The documents migration checks for it.

The hazardous state is recreated faithfully on a scratch database: a database
created under musl records a NULL collation version, so nulling it on a glibc
server reproduces exactly what a reattached volume looks like.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.migrations_helpers import refuse_reattached_musl_volume
from tests.conftest import TEST_DATABASE_URL
from tests.integration.conftest import BACKEND_DIR, _create_database_if_missing

pytestmark = pytest.mark.integration

SCRATCH_URL = make_url(TEST_DATABASE_URL).set(
    database=f"{make_url(TEST_DATABASE_URL).database}_collation_guard"
)


async def _admin(sql: str, params: dict[str, str] | None = None) -> None:
    engine = create_async_engine(SCRATCH_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(sql), params or {})
    finally:
        await engine.dispose()


@pytest.fixture
async def scratch() -> AsyncIterator[AsyncEngine]:
    """A fresh, empty database, dropped afterwards."""
    name = SCRATCH_URL.database
    await _admin(f'DROP DATABASE IF EXISTS "{name}"')
    await _create_database_if_missing(SCRATCH_URL.render_as_string(hide_password=False))
    engine = create_async_engine(SCRATCH_URL)
    try:
        yield engine
    finally:
        await engine.dispose()
        await _admin(f'DROP DATABASE IF EXISTS "{name}"')


async def simulate_reattached_musl_volume() -> None:
    """Needs superuser, as editing pg_database always does."""
    try:
        await _admin(
            "UPDATE pg_database SET datcollversion = NULL WHERE datname = :name",
            {"name": str(SCRATCH_URL.database)},
        )
    except Exception as exc:  # pragma: no cover - depends on the test server's role
        pytest.skip(f"cannot edit pg_database as this role: {type(exc).__name__}")


async def test_a_database_created_on_this_server_passes(scratch: AsyncEngine) -> None:
    async with scratch.connect() as connection:
        await connection.run_sync(refuse_reattached_musl_volume)


async def test_a_reattached_musl_volume_is_refused(scratch: AsyncEngine) -> None:
    await simulate_reattached_musl_volume()
    async with scratch.connect() as connection:
        with pytest.raises(RuntimeError, match="initialised under musl"):
            await connection.run_sync(refuse_reattached_musl_volume)


async def test_alembic_stops_before_changing_anything(scratch: AsyncEngine) -> None:
    """The guard runs inside the real migration, and the failed upgrade leaves
    the database exactly as empty as it found it."""
    await simulate_reattached_musl_volume()
    # In a thread: the migration takes seconds, and the test's event loop
    # should not be blocked for them.
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": SCRATCH_URL.render_as_string(hide_password=False)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Refusing to migrate" in result.stderr
    assert "docs/deployment.md" in result.stderr

    async with scratch.connect() as connection:
        tables = (
            await connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ).all()
    # Alembic runs the whole upgrade in one transaction; PostgreSQL DDL is
    # transactional, so the earlier revisions are rolled back too.
    assert tables == []
