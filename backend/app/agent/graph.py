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
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.context import AgentContext
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
) -> CompiledStateGraph[AgentState, AgentContext, AgentState, AgentState]:
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


async def run_agent(
    graph: CompiledStateGraph[AgentState, AgentContext, AgentState, AgentState],
    *,
    context: AgentContext,
    user_message: str,
) -> AgentResult:
    """Execute one turn under the total-run deadline.

    The conversation id is the thread id, so a checkpointed thread and a
    conversation are the same history. Identity and clients ride in ``context``
    and are never written to the checkpoint.
    """
    state = initial_state(
        conversation_id=str(context.conversation_id),
        run_id=str(context.run_id),
        prompt_version=PROMPT_VERSION,
    )
    state["messages"] = [HumanMessage(content=user_message)]

    config: RunnableConfig = {"configurable": {"thread_id": str(context.conversation_id)}}
    try:
        async with asyncio.timeout(context.limits.run_timeout_seconds):
            final = await graph.ainvoke(state, config=config, context=context)
    except TimeoutError:
        # The deadline fired mid-graph, so finalize never ran. Build the result
        # here rather than leaving the caller to interpret a raised exception.
        logger.warning(
            "run_timeout run_id=%s seconds=%s",
            context.run_id,
            context.limits.run_timeout_seconds,
        )
        return AgentResult(answer="", error=RUN_TIMEOUT_ERROR)

    # With the default stream mode, ainvoke returns the final state values;
    # LangGraph types that as a plain dict, so narrow it to our schema here.
    return _result_from_state(cast(AgentState, final))
