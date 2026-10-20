"""Text embeddings, behind an interface.

One implementation ships: OpenAI's embeddings endpoint through the official
``openai`` SDK. It is configured apart from the chat model (ADR-0015) because
``LLM_BASE_URL`` may name a provider with no embeddings API.

Nothing above this module may import ``openai`` or handle an SDK exception.
Failures leave here as :class:`EmbeddingTransientError` (worth one retry) or
:class:`EmbeddingPermanentError` (retrying only burns quota).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.core.config import Settings

if TYPE_CHECKING:
    # openai 3.x ships its own HTTP stack, httpx2, separate from the httpx the
    # rest of the app uses. Only the optional test client is typed with it.
    import httpx2

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """Vectors in the same order as the input texts, plus what they cost."""

    vectors: list[list[float]]
    # Billed input tokens, as reported by the provider. Feeds the per-user
    # daily embedding quota, so it is the provider's count, not an estimate.
    tokens: int


class EmbeddingError(Exception):
    """Base for every failure this module raises."""


class EmbeddingTransientError(EmbeddingError):
    """Timeout, connection reset, rate limit, or a 5xx: one bounded retry is sane."""


class EmbeddingPermanentError(EmbeddingError):
    """A rejected request, exhausted quota, or a malformed response."""


class EmbeddingAuthError(EmbeddingPermanentError):
    """The provider rejected our credentials (401/403). Only an operator can fix it."""


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def max_batch_size(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed every text in one request.

        Documents and queries go through the same call: this model embeds both
        the same way, so a second method would be a distinction without a
        difference.
        """
        ...

    async def aclose(self) -> None: ...


_TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_AUTH_STATUSES = frozenset({401, 403})
_TRANSIENT_NAMES = ("timeout", "connection", "apiconnection", "internalserver")
# OpenAI answers 429 both for "slow down" and for "your account is out of
# credit". The first clears on its own; the second never does, so retrying it
# would only repeat the failure.
_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached"})


def classify_embedding_error(exc: Exception) -> EmbeddingError:
    """Map an SDK exception onto the retry decision.

    By HTTP status where there is one, by exception name otherwise. Anything
    unrecognised is permanent: retrying an error we do not understand is how
    a bad request becomes a bill.
    """
    if isinstance(exc, TimeoutError):
        return EmbeddingTransientError("embedding request timed out")

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if getattr(exc, "code", None) in _QUOTA_CODES:
            return EmbeddingPermanentError("embedding provider quota exhausted")
        if status in _TRANSIENT_STATUSES:
            return EmbeddingTransientError(f"embedding provider returned {status}")
        if status in _AUTH_STATUSES:
            return EmbeddingAuthError(f"embedding provider rejected the credentials ({status})")
        return EmbeddingPermanentError(f"embedding provider returned {status}")

    name = type(exc).__name__.lower()
    if any(token in name for token in _TRANSIENT_NAMES):
        return EmbeddingTransientError(f"embedding transport failure: {type(exc).__name__}")
    return EmbeddingPermanentError(f"embedding call failed: {type(exc).__name__}")


def validate_inputs(texts: Sequence[str], *, max_batch_size: int) -> None:
    """Reject inputs that are our bug, before they cost a request.

    These are programming errors in the caller (the chunker never produces an
    empty chunk, and ingestion batches by ``max_batch_size``), so they raise
    ``ValueError`` rather than a provider error a caller might retry.
    """
    if not texts:
        raise ValueError("embed() needs at least one text")
    if len(texts) > max_batch_size:
        raise ValueError(f"embed() got {len(texts)} texts; the batch limit is {max_batch_size}")
    if any(not text.strip() for text in texts):
        raise ValueError("embed() got an empty text")


def validate_vectors(vectors: list[list[float]], *, expected: int, dimensions: int) -> None:
    """Refuse a response that cannot be stored or compared.

    A vector of the wrong width would fail the insert; a non-finite one would
    poison every distance computed against it. Either is the provider's fault,
    and retrying will not change it.
    """
    if len(vectors) != expected:
        raise EmbeddingPermanentError(
            f"embedding provider returned {len(vectors)} vectors for {expected} texts"
        )
    for vector in vectors:
        if len(vector) != dimensions:
            raise EmbeddingPermanentError(
                f"embedding provider returned {len(vector)} dimensions, expected {dimensions}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingPermanentError("embedding provider returned a non-finite value")


class OpenAIEmbeddings:
    """Embeddings from OpenAI's ``/v1/embeddings``."""

    def __init__(self, settings: Settings, http_client: httpx2.AsyncClient | None = None) -> None:
        # Imported lazily so a test suite using the fake does not load the SDK.
        from openai import AsyncOpenAI

        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._max_batch_size = settings.embedding_batch_size
        self._configured = bool(settings.embedding_api_key)
        self._client = AsyncOpenAI(
            api_key=settings.embedding_api_key or "not-set",
            base_url=settings.embedding_base_url,
            timeout=settings.embedding_timeout_seconds,
            # The SDK would otherwise retry on its own. The caller owns the one
            # retry the contract allows, so it is never multiplied.
            max_retries=0,
            # Tests pass a client on an httpx.MockTransport; nothing else does.
            http_client=http_client,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        validate_inputs(texts, max_batch_size=self._max_batch_size)
        if not self._configured:
            # Known before any request: a development machine without a key
            # gets a clear failure, not a 401 round-trip that looks like a
            # revoked credential.
            raise EmbeddingAuthError("EMBEDDING_API_KEY is not set")
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=list(texts),
                dimensions=self._dimensions,
                encoding_format="float",
            )
        except Exception as exc:
            raise classify_embedding_error(exc) from exc

        # The API documents results in input order and also labels each with
        # its index. Sorting by the label makes the order a checked fact.
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in ordered]
        validate_vectors(vectors, expected=len(texts), dimensions=self._dimensions)
        return EmbeddingBatch(vectors=vectors, tokens=response.usage.prompt_tokens)

    async def aclose(self) -> None:
        await self._client.close()


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    return OpenAIEmbeddings(settings)
