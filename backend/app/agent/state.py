"""The typed state a single agent run carries through the graph.

Two rules shape everything here.

First, state is **checkpointed**, so every value must survive a round-trip
through LangGraph's serializer. That is why sources and citations are
``TypedDict``s of JSON-native values rather than dataclasses: custom classes
currently round-trip with a deprecation warning and are slated to be rejected
outright, and a checkpoint that cannot be read back is a run that cannot be
recovered.

Second, state holds *data*. Provider clients, credentials, and the caller's
identity travel in the runtime context (``app/agent/context.py``) precisely so
they are never written to a checkpoint row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class Source(TypedDict):
    """A normalized search result, ready to be cited.

    ``source_id`` is the label the model is told to cite (``S1``, ``S2``, …).
    It is assigned by us, never by the provider and never by the model.
    """

    source_id: str
    url: str
    display_url: str
    title: str
    snippet: str
    publisher: str | None
    published_at: datetime | None
    retrieved_at: datetime


class Citation(TypedDict):
    """A source the finished answer actually cites, in first-use order."""

    ordinal: int
    source_id: str
    url: str
    display_url: str
    title: str
    publisher: str | None
    snippet: str
    published_at: datetime | None
    retrieved_at: datetime


ErrorCode = Literal[
    "search_unavailable",
    "llm_unavailable",
    "run_timeout",
    "internal_error",
]


class RunError(TypedDict):
    """Terminal error metadata. Safe to show a user and to log."""

    code: ErrorCode
    message: str
    retryable: bool


class AgentState(TypedDict, total=False):
    """Everything a run needs, and nothing it does not.

    Declared ``total=False`` because nodes return partial updates; the graph
    merges them. ``messages`` is the only reduced channel.
    """

    messages: Annotated[list[AnyMessage], add_messages]

    conversation_id: str
    run_id: str

    # Why the router went the way it did. Written for a log line and for tests,
    # not for the user: it is a one-line justification, never reasoning tokens.
    search_decision: bool
    search_rationale: str

    queries: list[str]
    sources: list[Source]
    citations: list[Citation]
    search_iterations: int

    answer: str
    # A searched answer that makes factual claims but carries no valid citation.
    # The user is told the sources could not be verified rather than shown an
    # answer that merely looks sourced.
    degraded: bool
    # One plain sentence explaining a degradation, shown to the user. Empty when
    # the run is healthy.
    notice: str
    error: RunError | None

    model: str
    prompt_version: str
    prompt_tokens: int | None
    completion_tokens: int | None


def initial_state(
    *,
    conversation_id: str,
    run_id: str,
    prompt_version: str,
) -> AgentState:
    """A fully populated starting state, so no node reads a missing key."""
    return AgentState(
        conversation_id=conversation_id,
        run_id=run_id,
        search_decision=False,
        search_rationale="",
        queries=[],
        sources=[],
        citations=[],
        search_iterations=0,
        answer="",
        degraded=False,
        notice="",
        error=None,
        model="",
        prompt_version=prompt_version,
        prompt_tokens=None,
        completion_tokens=None,
    )


def trim_history(messages: list[AnyMessage], *, max_messages: int) -> list[AnyMessage]:
    """Keep the newest turns and drop the oldest.

    Recency beats completeness: the latest turns carry the question being asked,
    and the system constraints are re-attached by the prompt builder on every
    call rather than being kept in the history. The newest message is never
    dropped or truncated — silently cutting the question the user just asked is
    the one failure the user is guaranteed to notice.
    """
    if max_messages <= 0:
        return messages[-1:] if messages else []
    if len(messages) <= max_messages:
        return list(messages)
    return list(messages[-max_messages:])
