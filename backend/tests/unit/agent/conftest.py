"""Fixtures for the agent tests.

Every test here drives the real compiled graph with fake providers, so the
transitions, budgets, and citation rules under test are the ones that ship.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from langchain_core.messages import HumanMessage

from app.agent.context import AgentContext, AgentLimits
from app.agent.graph import AgentResult, build_graph, run_agent
from app.providers.llm import LLMProvider
from app.providers.search import SearchProvider

DEFAULT_LIMITS = AgentLimits(
    max_searches_per_run=3,
    max_results_per_search=5,
    search_timeout_seconds=20.0,
    run_timeout_seconds=90.0,
    max_output_tokens=1_500,
    max_context_messages=20,
)


def make_context(
    llm: LLMProvider,
    search: SearchProvider,
    *,
    limits: AgentLimits = DEFAULT_LIMITS,
) -> AgentContext:
    return AgentContext(
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        llm=llm,
        search=search,
        limits=limits,
        request_id="test-request",
    )


@pytest.fixture
def run() -> Callable[..., object]:
    """Run one turn through a freshly compiled, uncheckpointed graph."""
    graph = build_graph()

    async def _run(
        message: str,
        *,
        llm: LLMProvider,
        search: SearchProvider,
        limits: AgentLimits = DEFAULT_LIMITS,
    ) -> AgentResult:
        return await run_agent(
            graph,
            context=make_context(llm, search, limits=limits),
            history=[HumanMessage(content=message)],
        )

    return _run
