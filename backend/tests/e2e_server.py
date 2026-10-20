"""The API as Playwright drives it: the real app, scripted providers.

    uv run uvicorn tests.e2e_server:app --port 8010

Everything is the production code path (auth, runs, SSE, persistence,
cancellation, PDF parsing and ingestion) except the paid dependencies (the
model, search, and embeddings), which are replaced by deterministic
stand-ins, and email, which is kept in memory so a spec can
open the confirmation link it was sent. It lives under tests/ so the stand-ins
and the outbox route never ship in an image.

On import it points at its own database (``E2E_DATABASE_URL``, or the dev
database's name plus ``_e2e``), migrates it with Alembic, and empties it, so
every Playwright run starts clean and never touches development data.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence

from fastapi import FastAPI, HTTPException
from langchain_core.messages import AnyMessage, HumanMessage

import app.main as app_main
from app.agent.routing import SearchDecision
from app.core.config import Environment, Settings
from app.providers.email import EmailPayload
from app.providers.llm import LLMChunk, StructuredT
from app.providers.search import SearchHit
from tests.fakes import FakeEmbeddings
from tests.scratch_database import prepare_scratch_database, scratch_database_url

# Words the scripted model reacts to, so each spec can pick its path.
SLOW_TRIGGER = "slow"

CITED_ANSWER = (
    "The central bank held its policy rate at 5.25% at its latest meeting [S1]. "
    "Most analysts expect the first cut later this year [S2]."
)
DIRECT_ANSWER = "Hello! Ask me anything. For current events I search the web and cite sources."
# Given whenever the prompt holds a passage of the user's document.
DOCUMENT_ANSWER = "The report says quarterly revenue grew twelve percent [D1]."
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
        has_documents = any("[D1] document:" in str(m.content) for m in messages)
        if SLOW_TRIGGER in question.lower():
            answer, delay = SLOW_ANSWER, 0.1
        elif has_documents:
            answer, delay = DOCUMENT_ANSWER, 0.02
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


class Outbox:
    """Keeps the last email sent to each address."""

    def __init__(self) -> None:
        self.latest: dict[str, EmailPayload] = {}

    async def send(self, payload: EmailPayload) -> None:
        self.latest[payload.to] = payload


def build() -> FastAPI:
    database_url = scratch_database_url(env_var="E2E_DATABASE_URL", suffix="_e2e")
    prepare_scratch_database(database_url)
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
        rate_limit_document_uploads=10_000,
        # The hashed bag-of-words fake scores far lower than a real model;
        # a question sharing one word with a passage is around 0.1.
        document_min_similarity=0.05,
        document_ingestion_sweep_seconds=1.0,
    )
    # The lifespan builds providers through these; swap them before it runs.
    app_main.build_llm_provider = lambda _settings: ScriptedLLM()  # type: ignore[assignment]
    app_main.build_search_provider = lambda _settings: ScriptedSearch()  # type: ignore[assignment]
    app_main.build_embedding_provider = lambda _settings: FakeEmbeddings()  # type: ignore[assignment]
    outbox = Outbox()
    app_main.build_email_sender = lambda _settings: outbox  # type: ignore[assignment]
    application = app_main.create_app(settings)

    @application.get("/__e2e/latest-email", include_in_schema=False)
    async def latest_email(to: str) -> dict[str, str]:
        """The last message sent to ``to``, for a spec to follow its link."""
        payload = outbox.latest.get(to)
        if payload is None:
            raise HTTPException(status_code=404)
        return {"subject": payload.subject, "body": payload.body}

    return application


app = build()
