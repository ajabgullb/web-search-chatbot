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
from typing import Protocol

from app.agent.tools.documents import DocumentSearch
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
    # Document retrieval (ADR-0015). Defaults keep tests that build limits by
    # hand, before documents existed, unchanged.
    document_min_similarity: float = 0.25
    document_top_k: int = 6
    document_context_chars: int = 12_000
    document_candidates: int = 20
    document_query_timeout_seconds: float = 10.0

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentLimits:
        return cls(
            max_searches_per_run=settings.max_searches_per_run,
            max_results_per_search=settings.max_results_per_search,
            search_timeout_seconds=settings.search_timeout_seconds,
            run_timeout_seconds=settings.run_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            max_context_messages=settings.max_context_messages,
            document_min_similarity=settings.document_min_similarity,
            document_top_k=settings.document_top_k,
            document_context_chars=settings.document_context_chars,
            document_candidates=settings.document_candidates,
            document_query_timeout_seconds=settings.document_query_timeout_seconds,
        )


class DocumentRetriever(Protocol):
    """Search one conversation's documents. Implemented over PostgreSQL in
    ``app/services/retrieval.py``; tests and evals script it."""

    async def ready_documents(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[str]: ...

    async def search(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        query: str,
        limits: AgentLimits,
    ) -> DocumentSearch: ...


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
    # The conversation's documents (ADR-0015). ``ready_documents`` holds the
    # filenames that can be searched, read once when the run starts; empty
    # means the graph never routes to document retrieval.
    documents: DocumentRetriever | None = None
    ready_documents: tuple[str, ...] = ()
