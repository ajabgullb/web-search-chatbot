"""Searching a conversation's documents for a question (ADR-0015).

The agent reaches this through ``AgentContext.documents``; nodes never touch
the database themselves. Which chunks win is decided by the pure functions in
``app/agent/tools/documents.py``; this module supplies their inputs.

A failure to embed the question is not a failed run. The search reports
``failed`` and the answer goes ahead without the documents, saying so.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable

from app.agent.context import AgentLimits
from app.agent.tools.documents import (
    DocumentSearch,
    exact_terms,
    fuse,
    keyword_query,
    keyword_terms,
    select,
)
from app.db.session import Database
from app.models.embedding_usage import EmbeddingPurpose
from app.providers.embeddings import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingTransientError,
)
from app.repositories.documents import DocumentRepository

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 0.5


class PostgresDocumentRetriever:
    def __init__(self, database: Database, embeddings: Callable[[], EmbeddingProvider]) -> None:
        self._database = database
        # Resolved per call, so a test can swap the provider on a running app.
        self._embeddings = embeddings

    async def ready_documents(self, *, user_id: uuid.UUID, conversation_id: uuid.UUID) -> list[str]:
        """Filenames of the documents a question in this conversation can read."""
        async with self._database.open_session() as session:
            return await DocumentRepository(session).ready_filenames(
                user_id=user_id,
                conversation_id=conversation_id,
                embedding_model=self._embeddings().model,
            )

    async def search(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        query: str,
        limits: AgentLimits,
    ) -> DocumentSearch:
        provider = self._embeddings()
        started = time.monotonic()
        try:
            async with asyncio.timeout(limits.document_query_timeout_seconds):
                batch = await self._embed_with_retry(provider, query)
        except (EmbeddingError, TimeoutError) as exc:
            logger.warning("document_search_unavailable", extra={"error_type": type(exc).__name__})
            return DocumentSearch(failed=True)

        vector = batch.vectors[0]
        terms = keyword_terms(query)
        async with self._database.open_session() as session:
            repository = DocumentRepository(session)
            await repository.record_usage(
                user_id=user_id, purpose=EmbeddingPurpose.query, tokens=batch.tokens
            )
            nearest = await repository.nearest_chunks(
                user_id=user_id,
                conversation_id=conversation_id,
                embedding_model=provider.model,
                vector=vector,
                limit=limits.document_candidates,
            )
            matched = (
                await repository.keyword_chunks(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    embedding_model=provider.model,
                    vector=vector,
                    tsquery=keyword_query(terms),
                    limit=limits.document_candidates,
                )
                if terms
                else []
            )
            await session.commit()

        chosen = select(
            fuse(nearest, matched),
            min_similarity=limits.document_min_similarity,
            top_k=limits.document_top_k,
            max_chars=limits.document_context_chars,
            exact=exact_terms(terms),
        )
        logger.info(
            "document_search",
            extra={
                "duration_ms": round((time.monotonic() - started) * 1000),
                "candidates": len(nearest) + len(matched),
                "chosen": len(chosen),
                # Similarities, not text: enough to tune the floor from logs.
                "best_similarity": round(nearest[0].similarity, 3) if nearest else None,
            },
        )
        return DocumentSearch(chunks=chosen)

    async def _embed_with_retry(self, provider: EmbeddingProvider, query: str) -> EmbeddingBatch:
        try:
            return await provider.embed([query])
        except EmbeddingTransientError:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
            return await provider.embed([query])
