"""A clean, migrated database of its own for a long-running test server.

The e2e and load-test servers each run the real app against a database named
after the developer's (``<name>_e2e``, ``<name>_load``), so they can never
touch development data, and empty it on start so every run begins clean.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from tests.integration.conftest import _create_database_if_missing

BACKEND_DIR = Path(__file__).resolve().parents[1]


def scratch_database_url(*, env_var: str, suffix: str) -> str:
    """``env_var`` if set, else the configured database's name plus ``suffix``."""
    configured = os.environ.get(env_var)
    if configured:
        return configured
    url = make_url(str(get_settings().database_url))
    return url.set(database=f"{url.database}{suffix}").render_as_string(hide_password=False)


def prepare_scratch_database(database_url: str) -> None:
    """Create if missing, migrate to head with Alembic, and empty."""
    asyncio.run(_create_database_if_missing(database_url))
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if migrated.returncode != 0:
        raise SystemExit(f"scratch database migration failed:\n{migrated.stderr}")
    asyncio.run(_empty(database_url))


async def _empty(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE users, auth_sessions, password_reset_tokens, conversations, "
                    "messages RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await engine.dispose()
