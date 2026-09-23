"""The compiled agent graph and the runner that enforces the run deadline.

    START -> decide_search -> web_search -> synthesize -> finalize -> END
                           \\              \\           /
                            -> answer_direct ----------

There is no back edge. Every path from START reaches ``finalize`` in at most
four steps, so a run cannot loop; the number of searches is bounded inside
``web_search`` by its own counter rather than by repeating a node.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.messages import AnyMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.graph.state import CompiledStateGraph

from app.agent.context import AgentContext
from app.agent.events import (
    AgentEvent,
    CitationAdded,
    SearchCompleted,
    SearchStarted,
    StatusChanged,
    TextDelta,
)
from app.agent.nodes import answer_direct, decide_search, finalize, synthesize, web_search
from app.agent.prompts import PROMPT_VERSION
from app.agent.routing import route_after_decision, route_after_search
from app.agent.state import AgentState, Citation, RunError, Source, initial_state

logger = logging.getLogger(__name__)


def _route_decide(state: AgentState) -> str:
    return route_after_decision(state.get("search_decision", False))


def _route_search(state: AgentState) -> str:
    return route_after_search(len(state.get("sources", [])))


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> AgentGraph:
    """Compile the graph. Built once at startup, never per request."""
    builder: StateGraph[AgentState, AgentContext, AgentState, AgentState] = StateGraph(
        AgentState, context_schema=AgentContext
    )
    builder.add_node("decide_search", decide_search)
    builder.add_node("web_search", web_search)
    builder.add_node("synthesize", synthesize)
    builder.add_node("answer_direct", answer_direct)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "decide_search")
    builder.add_conditional_edges(
        "decide_search",
        _route_decide,
        {"web_search": "web_search", "answer_direct": "answer_direct"},
    )
    builder.add_conditional_edges(
        "web_search",
        _route_search,
        {"synthesize": "synthesize", "answer_direct": "answer_direct"},
    )
    builder.add_edge("synthesize", "finalize")
    builder.add_edge("answer_direct", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """The stable public shape of a finished run.

    Callers read this; they never read graph state. That keeps the state free
    to change without breaking the API layer.
    """

    answer: str
    citations: list[Citation] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    searched: bool = False
    search_count: int = 0
    search_rationale: str = ""
    degraded: bool = False
    notice: str = ""
    error: RunError | None = None
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


RUN_TIMEOUT_ERROR = RunError(
    code="run_timeout",
    message="The assistant took too long to answer. Please try again.",
    retryable=True,
)


def _result_from_state(state: AgentState) -> AgentResult:
    return AgentResult(
        answer=state.get("answer", ""),
        citations=state.get("citations", []),
        sources=state.get("sources", []),
        searched=state.get("search_decision", False),
        search_count=state.get("search_iterations", 0),
        search_rationale=state.get("search_rationale", ""),
        degraded=state.get("degraded", False),
        notice=state.get("notice", ""),
        error=state.get("error"),
        model=state.get("model", ""),
        prompt_version=state.get("prompt_version", PROMPT_VERSION),
        prompt_tokens=state.get("prompt_tokens"),
        completion_tokens=state.get("completion_tokens"),
    )


AgentGraph = CompiledStateGraph[AgentState, AgentContext, AgentState, AgentState]

_EVENT_TYPES = (StatusChanged, SearchStarted, SearchCompleted, TextDelta, CitationAdded)


def _run_input(context: AgentContext, history: Sequence[AnyMessage]) -> AgentState:
    """The state a run starts from.

    ``history`` is the conversation as the application database records it,
    ending with the message being answered. It *replaces* the checkpoint's
    transcript rather than appending to it: the ``messages`` table is the one
    canonical history, so a failed run, a cancelled run, or a retry of the
    same question can never leave the thread holding a turn the product does
    not show. The checkpoint keeps its real job — recording each step of a run.
    """
    state = initial_state(
        conversation_id=str(context.conversation_id),
        run_id=str(context.run_id),
        prompt_version=PROMPT_VERSION,
    )
    # The add_messages reducer understands RemoveMessage, but LangChain's
    # AnyMessage union does not list it; the cast bridges that typing gap only.
    clear_transcript = cast(AnyMessage, RemoveMessage(id=REMOVE_ALL_MESSAGES))
    state["messages"] = [clear_transcript, *history]
    return state


async def stream_agent(
    graph: AgentGraph,
    *,
    context: AgentContext,
    history: Sequence[AnyMessage],
) -> AsyncIterator[AgentEvent | AgentResult]:
    """Run one turn, yielding progress events and then exactly one result.

    No deadline is applied here: the consumer owns the clock, because a
    timeout wrapped around a ``yield`` would also count time the consumer
    spends between items. See :func:`run_agent` and the run executor.
    """
    config: RunnableConfig = {"configurable": {"thread_id": str(context.conversation_id)}}
    final: AgentState | None = None

    async for mode, chunk in graph.astream(
        _run_input(context, history),
        config=config,
        context=context,
        stream_mode=["custom", "values"],
    ):
        if mode == "custom":
            # Only our own event types leave the graph. Anything else written
            # to the custom channel — by a future library node, say — is
            # dropped rather than forwarded somewhere it was never meant to go.
            if isinstance(chunk, _EVENT_TYPES):
                yield chunk
        else:
            # "values" is the full state after each step; the last one is the
            # finished run. LangGraph types it as a plain dict.
            final = cast(AgentState, chunk)

    if final is None:  # pragma: no cover - LangGraph always emits values
        yield AgentResult(answer="", error=INTERNAL_ERROR)
        return
    yield _result_from_state(final)


INTERNAL_ERROR = RunError(
    code="internal_error",
    message="Something went wrong while answering. Please try again.",
    retryable=True,
)


async def run_agent(
    graph: AgentGraph,
    *,
    context: AgentContext,
    history: Sequence[AnyMessage],
) -> AgentResult:
    """Execute one turn under the total-run deadline, discarding progress.

    The same code path as streaming, drained: there is one way to run the
    agent, and the tests exercise it.
    """
    result: AgentResult | None = None
    try:
        async with asyncio.timeout(context.limits.run_timeout_seconds):
            async for item in stream_agent(graph, context=context, history=history):
                if isinstance(item, AgentResult):
                    result = item
    except TimeoutError:
        # The deadline fired mid-graph, so finalize never ran. Build the result
        # here rather than leaving the caller to interpret a raised exception.
        logger.warning(
            "run_timeout run_id=%s seconds=%s",
            context.run_id,
            context.limits.run_timeout_seconds,
        )
        return AgentResult(answer="", error=RUN_TIMEOUT_ERROR)

    assert result is not None  # stream_agent always ends with a result
    return result
