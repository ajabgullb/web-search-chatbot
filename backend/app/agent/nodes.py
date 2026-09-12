"""The graph's nodes.

Each node takes the run's state and its runtime context and returns a partial
update. Nodes never construct a client, read configuration, or touch the
database: everything they need arrives through ``Runtime[AgentContext]``.

A node that cannot do its job records terminal error metadata rather than
raising, so the graph always reaches ``finalize`` and a run always ends with a
well-formed result.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from app.agent.citations import extract_citations, is_degraded
from app.agent.context import AgentContext
from app.agent.prompts import (
    ANSWER_DIRECT_SYSTEM,
    SEARCH_DECISION_SYSTEM,
    SYNTHESIZE_SYSTEM,
    render_sources_block,
)
from app.agent.routing import SearchDecision, heuristic_search_decision
from app.agent.state import AgentState, RunError, trim_history
from app.agent.tools.search import execute_searches, validate_queries
from app.providers.llm import LLMError, LLMTransientError

logger = logging.getLogger(__name__)

SEARCH_UNAVAILABLE_NOTICE = (
    "Web search was unavailable for this answer, so it comes from the model's "
    "own knowledge and could not be verified against sources."
)
UNVERIFIED_NOTICE = (
    "This answer could not be tied back to a specific source, so treat it as unverified."
)


def _last_user_message(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def _llm_error(exc: LLMError) -> RunError:
    return RunError(
        code="llm_unavailable",
        message="The language model is unavailable. Please try again.",
        retryable=isinstance(exc, LLMTransientError),
    )


async def decide_search(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Choose between searching and answering directly."""
    messages = state.get("messages", [])
    question = _last_user_message(messages)
    heuristic = heuristic_search_decision(question)

    if heuristic.should_search is not None:
        queries = [question] if heuristic.should_search else []
        return AgentState(
            search_decision=heuristic.should_search,
            search_rationale=f"heuristic: {heuristic.rationale}",
            queries=validate_queries(queries, limit=runtime.context.limits.max_searches_per_run),
        )

    history = trim_history(messages, max_messages=runtime.context.limits.max_context_messages)
    try:
        decision = await runtime.context.llm.structured(
            [SystemMessage(content=SEARCH_DECISION_SYSTEM), *history],
            SearchDecision,
        )
    except LLMError as exc:
        # The classifier is not worth failing the run over. Answering directly
        # is the cheaper wrong answer: it spends no search budget, and if the
        # model is genuinely down the answer node will report it properly.
        logger.warning("search_decision_failed error=%s", type(exc).__name__)
        return AgentState(
            search_decision=False,
            search_rationale="model: decision unavailable, answering directly",
            queries=[],
        )

    queries = decision.queries or ([question] if decision.should_search else [])
    return AgentState(
        search_decision=decision.should_search,
        search_rationale=f"model: {decision.rationale}"[:200],
        queries=validate_queries(queries, limit=runtime.context.limits.max_searches_per_run),
    )


async def web_search(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Run the bounded searches and normalize what comes back."""
    outcome = await execute_searches(
        state.get("queries", []),
        provider=runtime.context.search,
        limits=runtime.context.limits,
    )

    update = AgentState(
        sources=outcome.sources,
        queries=outcome.executed_queries,
        search_iterations=outcome.searches_run,
    )
    if outcome.provider_failed:
        # Not terminal. A direct answer is worth more to the user than an error
        # page, as long as they are told it is unverified.
        logger.warning("search_unavailable queries=%d", outcome.searches_run)
        update["degraded"] = True
        update["notice"] = SEARCH_UNAVAILABLE_NOTICE
    return update


async def synthesize(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Answer from the retrieved sources, then verify every citation."""
    sources = state.get("sources", [])
    history = trim_history(
        state.get("messages", []), max_messages=runtime.context.limits.max_context_messages
    )
    prompt = [
        SystemMessage(content=SYNTHESIZE_SYSTEM),
        SystemMessage(content=render_sources_block(sources)),
        *history,
    ]

    try:
        completion = await runtime.context.llm.complete(
            prompt, max_output_tokens=runtime.context.limits.max_output_tokens
        )
    except LLMError as exc:
        return AgentState(error=_llm_error(exc))

    answer, citations, discarded = extract_citations(completion.text, sources)
    degraded = is_degraded(searched=True, answer=answer, citations=citations)

    update = AgentState(
        answer=answer,
        citations=citations,
        model=completion.model,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        degraded=degraded or state.get("degraded", False),
    )
    if degraded:
        update["notice"] = UNVERIFIED_NOTICE
    if discarded:
        logger.warning("citations_invalid discarded=%d", len(discarded))
    return update


async def answer_direct(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Answer from the model's own knowledge, with no citations at all."""
    history = trim_history(
        state.get("messages", []), max_messages=runtime.context.limits.max_context_messages
    )
    prompt = [SystemMessage(content=ANSWER_DIRECT_SYSTEM), *history]

    try:
        completion = await runtime.context.llm.complete(
            prompt, max_output_tokens=runtime.context.limits.max_output_tokens
        )
    except LLMError as exc:
        return AgentState(error=_llm_error(exc))

    # No sources exist here, so validation strips every marker the model may
    # have produced out of habit. A citation with nothing behind it is the one
    # thing this pipeline must never render.
    answer, citations, discarded = extract_citations(completion.text, [])
    if discarded:
        logger.warning("uncited_markers_stripped count=%d", len(discarded))

    return AgentState(
        answer=answer,
        citations=citations,
        model=completion.model,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
    )


async def finalize(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Commit the answer to the transcript.

    Runs on every path, including failure, so that one node owns the shape of
    the result and callers never have to reconstruct it from partial state.
    """
    error = state.get("error")
    if error is not None:
        logger.info(
            "run_failed run_id=%s code=%s retryable=%s",
            state.get("run_id", ""),
            error["code"],
            error["retryable"],
        )
        return AgentState(messages=[])

    answer = state.get("answer", "")
    logger.info(
        "run_completed run_id=%s searched=%s searches=%d citations=%d degraded=%s",
        state.get("run_id", ""),
        state.get("search_decision", False),
        state.get("search_iterations", 0),
        len(state.get("citations", [])),
        state.get("degraded", False),
    )
    # Appended through the reducer so the checkpointed transcript matches what
    # the next turn in this thread will see.
    return AgentState(messages=[AIMessage(content=answer)])
