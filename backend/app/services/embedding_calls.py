"""One embedding request, as every caller makes it: timed, counted, retried once.

Ingestion and retrieval both embed text under the same rules (ADR-0015): a
transient failure gets one retry after a short pause, a permanent one none.
Keeping that, and the metrics, in one place means the two cannot drift.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Literal

from app.core import telemetry
from app.providers.embeddings import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingTransientError,
)

Purpose = Literal["document", "query"]

# The pause before the one retry. Public so tests can shorten it.
RETRY_BACKOFF_SECONDS = 0.5


async def _attempt(
    provider: EmbeddingProvider, texts: Sequence[str], purpose: Purpose
) -> EmbeddingBatch:
    started = time.monotonic()
    try:
        batch = await provider.embed(texts)
    except EmbeddingTransientError:
        telemetry.record_embedding(purpose, "transient_error", time.monotonic() - started)
        raise
    except EmbeddingError:
        telemetry.record_embedding(purpose, "permanent_error", time.monotonic() - started)
        raise
    telemetry.record_embedding(purpose, "ok", time.monotonic() - started, batch.tokens)
    return batch


async def embed_with_retry(
    provider: EmbeddingProvider, texts: Sequence[str], *, purpose: Purpose
) -> EmbeddingBatch:
    """Embed ``texts``; on a transient failure wait and try exactly once more."""
    try:
        return await _attempt(provider, texts, purpose)
    except EmbeddingTransientError:
        await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        return await _attempt(provider, texts, purpose)
