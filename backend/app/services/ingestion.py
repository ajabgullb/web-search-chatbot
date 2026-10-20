"""Background embedding of uploaded documents.

An upload stores its chunks with no vectors and returns at once; this loop
fills the vectors in. One loop runs in every API process, built in the
lifespan like the run manager, and they share the work through the database:

- **Claim.** A document is taken with ``FOR UPDATE SKIP LOCKED`` and held by a
  time lease, so two replicas never embed the same one, and nobody waits.
- **Resume.** Chunk text is already stored, so a process that dies part-way
  leaves nothing to redo but the missing vectors. Its lease runs out and the
  next sweep, here or on another replica, carries on from the first chunk
  still without one.
- **Bounded failure.** A transient provider error gets one immediate retry,
  then the document waits for a later sweep. Every claim counts as an
  attempt, and after ``DOCUMENT_EMBED_MAX_ATTEMPTS`` the document fails with
  ``embedding_unavailable``. A permanent error (bad key, exhausted account)
  fails it at once: retrying would only repeat the bill or the refusal.

Transactions stay short: none is open while a provider call is in flight.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.session import Database
from app.models.embedding_usage import EmbeddingPurpose
from app.providers.embeddings import (
    EmbeddingAuthError,
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingTransientError,
)
from app.repositories.documents import ClaimedDocument, DocumentRepository

logger = logging.getLogger(__name__)

EMBEDDING_UNAVAILABLE = "embedding_unavailable"

# The one retry the contract allows for a transient failure.
RETRY_BACKOFF_SECONDS = 1.0


class IngestionManager:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        # Resolved on every use rather than captured, so a test can swap the
        # provider on a running app, as it can the LLM and search providers.
        embeddings: Callable[[], EmbeddingProvider],
    ) -> None:
        self._database = database
        self._embeddings = embeddings
        self._lease = timedelta(seconds=settings.document_embed_lease_seconds)
        self._max_attempts = settings.document_embed_max_attempts
        self._sweep_seconds = settings.document_ingestion_sweep_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="document-ingestion")

    def wake(self) -> None:
        """A document was just uploaded; look now instead of at the next sweep."""
        self._wake.set()

    async def shutdown(self) -> None:
        """Stop between batches or mid-call. A held lease simply expires."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            # Cleared before draining, so a wake that arrives mid-drain is
            # not lost: the next wait returns at once and drains again.
            self._wake.clear()
            try:
                while await self.process_next():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad document must not end ingestion for the process.
                logger.exception("ingestion_loop_error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._sweep_seconds)

    # --- work --------------------------------------------------------------

    async def process_next(self) -> bool:
        """Claim one document and work on it. False when there was nothing to claim."""
        async with self._database.open_session() as session:
            claimed = await DocumentRepository(session).claim_next(
                now=datetime.now(UTC), lease=self._lease
            )
            await session.commit()
        if claimed is None:
            return False
        await self._embed_document(claimed)
        return True

    async def _embed_document(self, claimed: ClaimedDocument) -> None:
        started = time.monotonic()
        log = {"document_id": claimed.id, "attempt": claimed.attempts}
        if claimed.attempts > self._max_attempts:
            await self._fail(claimed, reason="attempts_exhausted")
            return

        provider = self._embeddings()
        while True:
            async with self._database.open_session() as session:
                pending = await DocumentRepository(session).pending_chunks(
                    document_id=claimed.id, limit=provider.max_batch_size
                )
            if not pending:
                async with self._database.open_session() as session:
                    ready = await DocumentRepository(session).mark_ready(document_id=claimed.id)
                    await session.commit()
                if ready:
                    logger.info(
                        "document_ready",
                        extra={**log, "duration_ms": round((time.monotonic() - started) * 1000)},
                    )
                return

            try:
                batch = await self._embed_with_retry(provider, [chunk.content for chunk in pending])
            except EmbeddingTransientError:
                # Not this sweep. The lease is pushed out so the provider gets
                # a rest, and the attempt counter decides when to give up.
                async with self._database.open_session() as session:
                    await DocumentRepository(session).back_off(
                        document_id=claimed.id,
                        until=datetime.now(UTC) + timedelta(seconds=self._sweep_seconds),
                    )
                    await session.commit()
                logger.warning("document_embedding_deferred", extra=log)
                return
            except EmbeddingError as exc:
                if isinstance(exc, EmbeddingAuthError):
                    logger.warning(
                        "embedding_auth_failed",
                        extra={"hint": "check EMBEDDING_API_KEY, then recreate the api container"},
                    )
                await self._fail(claimed, reason=type(exc).__name__)
                return

            # One transaction per batch: the vectors, what they cost, and a
            # fresh lease are recorded together or not at all. The document
            # row is locked first; see ``lock_for_batch``.
            async with self._database.open_session() as session:
                repository = DocumentRepository(session)
                still_there = await repository.lock_for_batch(
                    document_id=claimed.id,
                    tokens=batch.tokens,
                    lease_until=datetime.now(UTC) + self._lease,
                )
                if still_there:
                    await repository.store_embeddings(
                        vectors=[
                            (chunk.id, vector)
                            for chunk, vector in zip(pending, batch.vectors, strict=True)
                        ]
                    )
                # Recorded either way: the provider billed for it.
                await repository.record_usage(
                    user_id=claimed.user_id, purpose=EmbeddingPurpose.document, tokens=batch.tokens
                )
                await session.commit()
            if not still_there:
                logger.info("document_deleted_while_embedding", extra=log)
                return

    async def _embed_with_retry(
        self, provider: EmbeddingProvider, texts: list[str]
    ) -> EmbeddingBatch:
        try:
            return await provider.embed(texts)
        except EmbeddingTransientError:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
            return await provider.embed(texts)

    async def _fail(self, claimed: ClaimedDocument, *, reason: str) -> None:
        async with self._database.open_session() as session:
            await DocumentRepository(session).mark_failed(
                document_id=claimed.id, error_code=EMBEDDING_UNAVAILABLE
            )
            await session.commit()
        logger.warning(
            "document_failed",
            extra={"document_id": claimed.id, "attempt": claimed.attempts, "reason": reason},
        )
