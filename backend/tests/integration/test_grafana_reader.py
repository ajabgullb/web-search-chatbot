"""Grafana's database login can run every usage panel, and read nothing else.

Applies ``observability/postgres/grafana_reader.sql`` to the test database,
then runs each SQL panel of the committed dashboard as that role. The negative
cases are the point: message text, password hashes, sessions, and document
text must be refused by PostgreSQL itself, whatever a panel query asks for.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLE_SCRIPT = REPO_ROOT / "observability/postgres/grafana_reader.sql"
DASHBOARD = REPO_ROOT / "observability/grafana/dashboards/web-search-chatbot.json"


def _panel_queries() -> list[tuple[str, str]]:
    return [
        (panel["title"], target["rawSql"])
        for panel in json.loads(DASHBOARD.read_text())["panels"]
        for target in panel.get("targets", [])
        if target["datasource"]["uid"] == "appdb"
    ]


def _expand_grafana_macros(sql: str) -> str:
    """What Grafana does before sending the query, for the two macros used."""
    sql = re.sub(r"\$__timeFilter\((\w+)\)", r"\1 > now() - interval '30 days'", sql)
    return re.sub(r"\$\{\w+:sqlstring\}", "'0.5'", sql)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        # asyncpg's simple-query path: the script is several statements.
        await raw.driver_connection.execute(ROLE_SCRIPT.read_text())  # type: ignore[union-attr]
    try:
        yield engine
    finally:
        await engine.dispose()


async def _as_reader(engine: AsyncEngine, sql: str) -> None:
    async with engine.connect() as connection, connection.begin():
        await connection.execute(text("SET LOCAL ROLE grafana_reader"))
        await connection.execute(text(sql))


def test_the_dashboard_has_database_panels() -> None:
    assert len(_panel_queries()) >= 8


@pytest.mark.parametrize(("title", "sql"), _panel_queries(), ids=lambda value: str(value)[:40])
async def test_every_usage_panel_runs_as_the_reader(
    engine: AsyncEngine, title: str, sql: str
) -> None:
    await _as_reader(engine, _expand_grafana_macros(sql))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT content FROM messages",
        "SELECT notice FROM messages",
        "SELECT email FROM users",
        "SELECT password_hash FROM users",
        "SELECT * FROM users",
        "SELECT title FROM conversations",
        "SELECT filename FROM documents",
        "SELECT content FROM document_chunks",
        "SELECT * FROM auth_sessions",
        "SELECT * FROM password_reset_tokens",
        "SELECT * FROM citations",
        "SELECT idempotency_key FROM runs",
    ],
)
async def test_private_columns_and_tables_are_refused(engine: AsyncEngine, sql: str) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        await _as_reader(engine, sql)


async def test_the_reader_cannot_write(engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        await _as_reader(engine, "UPDATE runs SET search_count = 0")


async def test_reapplying_the_script_is_harmless(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        await raw.driver_connection.execute(ROLE_SCRIPT.read_text())  # type: ignore[union-attr]
    await _as_reader(engine, "SELECT count(*) FROM runs")
