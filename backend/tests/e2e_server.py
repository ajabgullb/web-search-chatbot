"""The API as Playwright drives it: the real app, scripted providers.

    uv run uvicorn tests.e2e_server:app --port 8010

Everything is the production code path (auth, runs, SSE, persistence,
cancellation) except the two paid dependencies, which are replaced by
deterministic stand-ins. It lives under tests/ so the stand-ins never ship in
an image.

On import it points at its own database (``E2E_DATABASE_URL``, or the dev
database's name plus ``_e2e``), migrates it with Alembic, and empties it, so
every Playwright run starts clean and never touches development data.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from fastapi import FastAPI
from langchain_core.messages import AnyMessage, HumanMessage
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import app.main as app_main
from app.agent.routing import SearchDecision
from app.core.config import Environment, Settings, get_settings
from app.providers.llm import LLMChunk, StructuredT
from app.providers.search import SearchHit
from tests.integration.conftest import _create_database_if_missing

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Words the scripted model reacts to, so each spec can pick its path.
SLOW_TRIGGER = "slow"

CITED_ANSWER = (
    "The central bank held its policy rate at 5.25% at its latest meeting [S1]. "
    "Most analysts expect the first cut later this year [S2]."
)
DIRECT_ANSWER = "Hello! Ask me anything. For current events I search the web and cite sources."
SLOW_ANSWER = " ".join(
    f"Gardening tip {n}: water deeply, mulch well, and watch the soil." for n in range(1, 60)
)

HITS = [
    SearchHit(
        url="https://news.example.com/rates",
        title="Central bank holds rates",
        snippet="The policy rate was held at 5.25 percent.",
        publisher="Example News",
    ),
    SearchHit(
        url="https://analysis.example.org/outlook",
        title="Rate outlook for the year",
        snippet="Analysts expect a first cut in the second half.",
    ),
]


class ScriptedLLM:
    """Answers by what it was asked, with no model and no cost."""

    @property
    def model(self) -> str:
        return "e2e-scripted"

    async def stream(
        self, messages: Sequence[AnyMessage], *, max_output_tokens: int | None = None
    ) -> AsyncIterator[LLMChunk]:
        question = next(
            (str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage)), ""
        )
        # The synthesis prompt carries the fenced sources; the direct one does not.
        has_sources = any("<<<untrusted-excerpt" in str(m.content) for m in messages)
        if SLOW_TRIGGER in question.lower():
            answer, delay = SLOW_ANSWER, 0.1
        elif has_sources:
            answer, delay = CITED_ANSWER, 0.02
        else:
            answer, delay = DIRECT_ANSWER, 0.02
        pieces = [answer[i : i + 12] for i in range(0, len(answer), 12)]
        for index, piece in enumerate(pieces):
            await asyncio.sleep(delay)
            last = index == len(pieces) - 1
            yield LLMChunk(
                text=piece,
                prompt_tokens=120 if last else None,
                completion_tokens=40 if last else None,
            )

    async def structured(
        self, messages: Sequence[AnyMessage], schema: type[StructuredT]
    ) -> StructuredT:
        # Only reached when the heuristics abstain: answer directly.
        decision = SearchDecision(should_search=False, rationale="e2e", queries=[])
        assert isinstance(decision, schema)
        return decision


class ScriptedSearch:
    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        await asyncio.sleep(0.05)
        return HITS[:max_results]

    async def aclose(self) -> None:
        return None


def _database_url() -> str:
    configured = os.environ.get("E2E_DATABASE_URL")
    if configured:
        return configured
    url = make_url(str(get_settings().database_url))
    return url.set(database=f"{url.database}_e2e").render_as_string(hide_password=False)


def _migrate(database_url: str) -> None:
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if migrated.returncode != 0:
        raise SystemExit(f"e2e database migration failed:\n{migrated.stderr}")


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


def build() -> FastAPI:
    database_url = _database_url()
    asyncio.run(_create_database_if_missing(database_url))
    _migrate(database_url)
    asyncio.run(_empty(database_url))
    settings = Settings(
        # Development: the refresh cookie is not Secure, because Playwright
        # talks plain http to the Vite server.
        environment=Environment.development,
        database_url=database_url,  # type: ignore[arg-type]
        frontend_origin=os.environ.get("E2E_FRONTEND_ORIGIN", "http://localhost:5174"),
        redis_url="",
        smtp_host="",
        log_level="WARNING",
        log_format="console",
        langsmith_tracing=False,
        # Every spec registers fresh users from one IP.
        rate_limit_login=10_000,
        rate_limit_register=10_000,
        rate_limit_forgot_password=10_000,
        rate_limit_runs=10_000,
    )
    # The lifespan builds providers through these; swap them before it runs.
    app_main.build_llm_provider = lambda _settings: ScriptedLLM()  # type: ignore[assignment]
    app_main.build_search_provider = lambda _settings: ScriptedSearch()  # type: ignore[assignment]
    return app_main.create_app(settings)


app = build()
