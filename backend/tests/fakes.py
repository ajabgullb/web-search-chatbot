"""Test doubles for the provider interfaces."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import AsyncIterator, Sequence
from datetime import datetime

from langchain_core.messages import AnyMessage

from app.agent.routing import SearchDecision
from app.core.config import EMBEDDING_VECTOR_DIMENSIONS
from app.providers.email import EmailPayload
from app.providers.embeddings import EmbeddingBatch, validate_inputs
from app.providers.llm import LLMChunk, StructuredT
from app.providers.search import SearchHit


class RecordingEmailSender:
    """Captures messages instead of sending them.

    Lets a test read the reset token out of the email body, which is the only
    place it ever exists in plaintext — the database stores just its hash.
    """

    def __init__(self) -> None:
        self.sent: list[EmailPayload] = []

    async def send(self, payload: EmailPayload) -> None:
        self.sent.append(payload)

    @property
    def last(self) -> EmailPayload:
        assert self.sent, "no email was sent"
        return self.sent[-1]


class FakeLLM:
    """A scripted language model.

    Deterministic by construction: it streams the next queued answer in small
    fixed-size chunks and records what it was asked. CI must never need a paid
    API, and a graph test that depends on a real model's wording tests the
    model, not the graph.

    Chunks are deliberately tiny (``chunk_size`` characters) so that citation
    markers and multi-byte characters are split across chunk boundaries the way
    a real token stream splits them.
    """

    def __init__(
        self,
        *,
        answers: list[str] | None = None,
        decision: SearchDecision | None = None,
        answer_error: Exception | None = None,
        fail_after_chunks: int | None = None,
        structured_error: Exception | None = None,
        model: str = "fake-model",
        delay_seconds: float = 0.0,
        chunk_size: int = 4,
        chunk_delay_seconds: float = 0.0,
    ) -> None:
        self._answers = list(answers or ["A fake answer."])
        # Defaults to "do not search" so a test that only cares about the
        # direct branch does not have to script a decision it never inspects.
        self._decision = decision or SearchDecision(
            should_search=False, rationale="fake default", queries=[]
        )
        self._answer_error = answer_error
        # With answer_error set: None fails before the first chunk, an integer
        # fails after that many chunks — the mid-stream outage.
        self._fail_after_chunks = fail_after_chunks
        self._structured_error = structured_error
        self._model = model
        self._delay_seconds = delay_seconds
        self._chunk_size = chunk_size
        self._chunk_delay_seconds = chunk_delay_seconds
        self.calls: list[list[AnyMessage]] = []
        self.structured_calls: list[list[AnyMessage]] = []
        self.chunks_sent = 0
        self.finished_streams = 0
        # Set when a stream is cancelled mid-flight: the proof that stopping a
        # run really stopped the (billable) generation, not just the display.
        self.cancelled = False

    @property
    def model(self) -> str:
        return self._model

    @property
    def last_prompt_text(self) -> str:
        """Every message of the most recent answer prompt, flattened."""
        assert self.calls, "the model was never called"
        return "\n".join(str(message.content) for message in self.calls[-1])

    async def stream(
        self, messages: Sequence[AnyMessage], *, max_output_tokens: int | None = None
    ) -> AsyncIterator[LLMChunk]:
        self.calls.append(list(messages))
        text = self._answers.pop(0) if self._answers else "A fake answer."
        pieces = [text[i : i + self._chunk_size] for i in range(0, len(text), self._chunk_size)]
        try:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            if self._answer_error is not None and self._fail_after_chunks is None:
                raise self._answer_error
            for index, piece in enumerate(pieces):
                if self._answer_error is not None and index == self._fail_after_chunks:
                    raise self._answer_error
                if self._chunk_delay_seconds:
                    await asyncio.sleep(self._chunk_delay_seconds)
                self.chunks_sent += 1
                last = index == len(pieces) - 1
                yield LLMChunk(
                    text=piece,
                    prompt_tokens=11 if last else None,
                    completion_tokens=7 if last else None,
                )
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished_streams += 1

    async def structured(
        self, messages: Sequence[AnyMessage], schema: type[StructuredT]
    ) -> StructuredT:
        self.structured_calls.append(list(messages))
        if self._structured_error is not None:
            raise self._structured_error
        assert isinstance(self._decision, schema), f"FakeLLM cannot produce a {schema.__name__}"
        return self._decision


class FakeSearchProvider:
    """A scripted search provider.

    ``results`` maps a query to its hits; ``default`` answers anything else.
    ``error`` makes every call fail, which is how the outage paths are tested.
    """

    def __init__(
        self,
        *,
        results: dict[str, list[SearchHit]] | None = None,
        default: list[SearchHit] | None = None,
        error: Exception | None = None,
        fail_times: int | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._results = results or {}
        self._default = default if default is not None else []
        self._error = error
        # None means "fail forever"; an integer fails that many calls and then
        # succeeds, which is how the retry path is exercised.
        self._fail_times = fail_times
        self._delay_seconds = delay_seconds
        self.queries: list[str] = []
        self.closed = False

    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        self.queries.append(query)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._error is not None and (
            self._fail_times is None or len(self.queries) <= self._fail_times
        ):
            raise self._error
        return self._results.get(query, self._default)[:max_results]

    async def aclose(self) -> None:
        self.closed = True


def hit(
    url: str,
    *,
    title: str = "A title",
    snippet: str = "A snippet of supporting evidence.",
    publisher: str | None = None,
    published_at: datetime | None = None,
) -> SearchHit:
    """Build a search hit without restating every field."""
    return SearchHit(
        url=url,
        title=title,
        snippet=snippet,
        publisher=publisher,
        published_at=published_at,
    )


_WORD = re.compile(r"\w+")


class FakeEmbeddings:
    """A deterministic embedder whose similarity means something.

    Each word is hashed to one dimension and a sign, and the counts are
    L2-normalised: a hashed bag of words. Texts that share words score as
    similar and texts that share none score near zero, so a retrieval test can
    assert which chunk ranks first without a model. ``blake2b`` rather than
    ``hash()``, which is salted per process and would change between runs.
    """

    def __init__(
        self,
        *,
        dimensions: int = EMBEDDING_VECTOR_DIMENSIONS,
        model: str = "fake-embedding",
        max_batch_size: int = 100,
        error: Exception | None = None,
        fail_times: int | None = None,
    ) -> None:
        self._dimensions = dimensions
        self._model = model
        self._max_batch_size = max_batch_size
        self._error = error
        # None means "fail forever"; an integer fails that many calls first.
        self._fail_times = fail_times
        self.calls: list[list[str]] = []
        self.closed = False

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    def vector(self, text: str) -> list[float]:
        values = [0.0] * self._dimensions
        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            values[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in values))
        # A text of only punctuation still needs a storable, finite vector.
        return [value / norm for value in values] if norm else values

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        validate_inputs(texts, max_batch_size=self._max_batch_size)
        self.calls.append(list(texts))
        if self._error is not None and (
            self._fail_times is None or len(self.calls) <= self._fail_times
        ):
            raise self._error
        tokens = sum(len(_WORD.findall(text)) for text in texts)
        return EmbeddingBatch(vectors=[self.vector(text) for text in texts], tokens=tokens)

    async def aclose(self) -> None:
        self.closed = True
