"""What a run reports while it is in progress.

Nodes emit these through LangGraph's custom stream channel
(``runtime.stream_writer``). They are the agent's *internal* vocabulary: plain
dataclasses with no HTTP, JSON, or sequence numbering in them. The service
layer maps them onto the public SSE protocol, so the protocol can be versioned
without touching a node, and a node cannot put anything on the wire that the
mapping does not explicitly carry.

Nothing here holds reasoning. ``StatusChanged`` is a coarse phase label, not a
narration of what the model is thinking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.agent.state import Citation, Source

AgentStatus = Literal["thinking", "searching", "writing"]


@dataclass(frozen=True, slots=True)
class StatusChanged:
    status: AgentStatus


@dataclass(frozen=True, slots=True)
class SearchStarted:
    # Already validated by ``validate_queries``: whitespace-collapsed and
    # length-capped. It is the user's own question or a model rewording of it.
    query: str


@dataclass(frozen=True, slots=True)
class SearchCompleted:
    query: str
    # The sources *this* query contributed after deduplication, so a client
    # can show "3 results" per search without double counting.
    sources: tuple[Source, ...]
    failed: bool = False


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class CitationAdded:
    """A source cited for the first time in the answer being written."""

    citation: Citation


AgentEvent = StatusChanged | SearchStarted | SearchCompleted | TextDelta | CitationAdded
