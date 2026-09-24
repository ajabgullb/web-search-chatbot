"""Runtime context: everything a run needs that must never be checkpointed.

LangGraph serializes state on every step. Provider clients cannot be
serialized, and credentials and user identity must not be — a checkpoint row is
long-lived storage, and anything written there outlives the request. So they
travel here instead, as a plain object handed to ``ainvoke(context=...)`` and
read by nodes through ``Runtime[AgentContext]``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.agent.tracing import TraceConfig
from app.core.config import Settings
from app.providers.llm import LLMProvider
from app.providers.search import SearchProvider


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """Every budget a run is allowed to spend, resolved from settings once."""

    max_searches_per_run: int
    max_results_per_search: int
    search_timeout_seconds: float
    run_timeout_seconds: float
    max_output_tokens: int
    max_context_messages: int

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentLimits:
        return cls(
            max_searches_per_run=settings.max_searches_per_run,
            max_results_per_search=settings.max_results_per_search,
            search_timeout_seconds=settings.search_timeout_seconds,
            run_timeout_seconds=settings.run_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            max_context_messages=settings.max_context_messages,
        )


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Identity, clients, and limits for one run."""

    user_id: uuid.UUID
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    llm: LLMProvider
    search: SearchProvider
    limits: AgentLimits
    # Correlates every log line of this run with the HTTP request that started
    # it. Optional because the graph is also driven directly by tests.
    request_id: str | None = None
    # Tags, version metadata, and (when tracing is on) the tracer callback.
    trace: TraceConfig | None = None
