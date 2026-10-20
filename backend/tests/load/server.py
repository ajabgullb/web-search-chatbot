"""The API under load: the real app, providers that take as long as real ones.

    python -m uvicorn tests.load.server:app --port 8020 --no-access-log

Started by ``tests/load/run.py``; rarely run by hand. Everything is the
production code path (auth, Argon2, runs, SSE, PostgreSQL, Redis locks and
limits) except the model and the search engine, which are replaced by
stand-ins that *wait* like the real ones but cost nothing. A load test of the
real providers measures their rate limits and your bill, not this server.

Latencies are deliberately on the slow side of typical, so streams stay open
about as long as they do in production, which is what consumes connections:

- search: 1.0 s per query
- model: 0.8 s to the first token, then ~600 characters over ~2.5 s
- embeddings: 0.3 s per request (a batch of chunks, or one question)

PDF parsing is real: the upload phase in ``run.py`` sends real PDFs through
the forkserver child processes, which is what that phase measures.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence

from fastapi import FastAPI
from langchain_core.messages import AnyMessage

import app.main as app_main
from app.agent.routing import SearchDecision
from app.core.config import Environment, Settings
from app.providers.email import EmailPayload
from app.providers.embeddings import EmbeddingBatch
from app.providers.llm import LLMChunk, StructuredT
from app.providers.search import SearchHit
from tests.fakes import FakeEmbeddings
from tests.scratch_database import prepare_scratch_database, scratch_database_url

SEARCH_SECONDS = 1.0
FIRST_TOKEN_SECONDS = 0.8
CHUNK_SECONDS = 0.05
CHUNK_CHARS = 12
EMBED_SECONDS = 0.3

CITED_ANSWER = (
    "According to the latest reports, the central bank held its policy rate steady [S1]. "
    "Analysts quoted in the coverage expect the first reduction later in the year, "
    "although they disagree about the timing [S2]. "
) * 2
DIRECT_ANSWER = (
    "Photosynthesis captures light in chlorophyll and uses its energy to split water, "
    "releasing oxygen and storing the energy in sugars built from carbon dioxide. "
) * 2

HITS = [
    SearchHit(
        url="https://news.example.com/rates",
        title="Central bank holds rates",
        snippet="The policy rate was held steady.",
        publisher="Example News",
    ),
    SearchHit(
        url="https://analysis.example.org/outlook",
        title="Rate outlook for the year",
        snippet="Analysts expect a first cut in the second half.",
    ),
]


class SlowLLM:
    @property
    def model(self) -> str:
        return "load-scripted"

    async def stream(
        self, messages: Sequence[AnyMessage], *, max_output_tokens: int | None = None
    ) -> AsyncIterator[LLMChunk]:
        # Cite only when the prompt carries sources, as a real model would;
        # markers in a direct answer are stripped and logged as anomalies.
        has_sources = any("<<<untrusted-excerpt" in str(m.content) for m in messages)
        answer = CITED_ANSWER if has_sources else DIRECT_ANSWER
        await asyncio.sleep(FIRST_TOKEN_SECONDS)
        pieces = [answer[i : i + CHUNK_CHARS] for i in range(0, len(answer), CHUNK_CHARS)]
        for index, piece in enumerate(pieces):
            if index:
                await asyncio.sleep(CHUNK_SECONDS)
            last = index == len(pieces) - 1
            yield LLMChunk(
                text=piece,
                prompt_tokens=900 if last else None,
                completion_tokens=150 if last else None,
            )

    async def structured(
        self, messages: Sequence[AnyMessage], schema: type[StructuredT]
    ) -> StructuredT:
        decision = SearchDecision(should_search=False, rationale="load", queries=[])
        assert isinstance(decision, schema)
        return decision


class SlowSearch:
    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        await asyncio.sleep(SEARCH_SECONDS)
        return HITS[:max_results]

    async def aclose(self) -> None:
        return None


def hash_on_event_loop() -> None:
    """Undo the off-loop hashing fix, to measure what it is worth.

    ``LOAD_HASH_ON_EVENT_LOOP=1`` makes sign-in verify passwords the way the
    code did before ADR-0013: synchronously, inside the async handler. Only
    the load test can turn this on; it lives in tests/, which never ships.
    """
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    from app.core import security
    from app.services import auth

    hasher = security._password_hasher

    async def verify_blocking(password: str, password_hash: str) -> bool:
        try:
            return hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    auth.verify_password = verify_blocking  # type: ignore[assignment]


class SlowEmbeddings(FakeEmbeddings):
    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        await asyncio.sleep(EMBED_SECONDS)
        return await super().embed(texts)


class DiscardEmail:
    async def send(self, payload: EmailPayload) -> None:
        return None


def build() -> FastAPI:
    if os.environ.get("LOAD_HASH_ON_EVENT_LOOP") == "1":
        hash_on_event_loop()
    database_url = scratch_database_url(env_var="LOAD_DATABASE_URL", suffix="_load")
    prepare_scratch_database(database_url)
    settings = Settings(
        environment=Environment.development,
        database_url=database_url,  # type: ignore[arg-type]
        # Production coordinates locks and limits through Redis, so the test
        # does too. A database index of its own keeps it apart from the
        # development stack's keys.
        redis_url=os.environ.get("LOAD_REDIS_URL", "redis://localhost:6379/15"),
        smtp_host="",
        log_level="ERROR",
        log_format="json",
        langsmith_tracing=False,
        # Every virtual user connects from 127.0.0.1, so per-IP limits would
        # measure the limiter rather than the server. Per-user and per-account
        # limits stay at their production defaults.
        rate_limit_login=1_000_000,
        rate_limit_register=1_000_000,
        rate_limit_forgot_password=1_000_000,
        # Verification is not what is being measured; it would only add a
        # step to every virtual user's setup.
        require_email_verification=False,
        # The upload phase rotates through accounts; the per-user upload
        # limit would otherwise be what it measures.
        rate_limit_document_uploads=1_000_000,
    )
    app_main.build_llm_provider = lambda _settings: SlowLLM()  # type: ignore[assignment]
    app_main.build_search_provider = lambda _settings: SlowSearch()  # type: ignore[assignment]
    app_main.build_email_sender = lambda _settings: DiscardEmail()  # type: ignore[assignment]
    app_main.build_embedding_provider = lambda _settings: SlowEmbeddings()  # type: ignore[assignment]
    return app_main.create_app(settings)


app = build()
