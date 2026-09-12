"""Test doubles for the provider interfaces."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

from langchain_core.messages import AnyMessage

from app.agent.routing import SearchDecision
from app.providers.email import EmailPayload
from app.providers.llm import LLMCompletion, StructuredT
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

    Deterministic by construction: it returns the next queued answer and records
    what it was asked. CI must never need a paid API, and a graph test that
    depends on a real model's wording tests the model, not the graph.
    """

    def __init__(
        self,
        *,
        answers: list[str] | None = None,
        decision: SearchDecision | None = None,
        complete_error: Exception | None = None,
        structured_error: Exception | None = None,
        model: str = "fake-model",
        delay_seconds: float = 0.0,
    ) -> None:
        self._answers = list(answers or ["A fake answer."])
        # Defaults to "do not search" so a test that only cares about the
        # direct branch does not have to script a decision it never inspects.
        self._decision = decision or SearchDecision(
            should_search=False, rationale="fake default", queries=[]
        )
        self._complete_error = complete_error
        self._structured_error = structured_error
        self._model = model
        self._delay_seconds = delay_seconds
        self.complete_calls: list[list[AnyMessage]] = []
        self.structured_calls: list[list[AnyMessage]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def last_prompt_text(self) -> str:
        """Every message of the most recent completion, flattened."""
        assert self.complete_calls, "the model was never called"
        return "\n".join(str(message.content) for message in self.complete_calls[-1])

    async def complete(
        self, messages: Sequence[AnyMessage], *, max_output_tokens: int | None = None
    ) -> LLMCompletion:
        self.complete_calls.append(list(messages))
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._complete_error is not None:
            raise self._complete_error
        text = self._answers.pop(0) if self._answers else "A fake answer."
        return LLMCompletion(text=text, model=self._model, prompt_tokens=11, completion_tokens=7)

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
