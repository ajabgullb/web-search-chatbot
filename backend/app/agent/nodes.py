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
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from app.agent.citations import CitationStreamFilter, extract_citations, is_degraded
from app.agent.context import AgentContext
from app.agent.events import AgentEvent, CitationAdded, StatusChanged, TextDelta
from app.agent.prompts import (
    ANSWER_DIRECT_SYSTEM,
    SEARCH_DECISION_SYSTEM,
    SYNTHESIZE_SYSTEM,
    render_sources_block,
)
from app.agent.routing import SearchDecision, heuristic_search_decision
from app.agent.state import AgentState, Citation, RunError, Source, trim_history
from app.agent.tools.search import execute_searches, validate_queries
from app.core import telemetry
from app.providers.llm import LLMError, LLMTransientError

logger = logging.getLogger(__name__)

SEARCH_UNAVAILABLE_NOTICE = (
    "Web search was unavailable for this answer, so it comes from the model's "
    "own knowledge and could not be verified against sources."
)
NO_SOURCES_NOTICE = (
    "Web search found no usable sources for this question, so this answer comes "
    "from the model's own knowledge and could not be verified against sources."
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
    telemetry.record_llm_failure("transient" if isinstance(exc, LLMTransientError) else "permanent")
    return RunError(
        code="llm_unavailable",
        message="The language model is unavailable. Please try again.",
        retryable=isinstance(exc, LLMTransientError),
    )


def _emit(runtime: Runtime[AgentContext], event: AgentEvent) -> None:
    """Report progress. A no-op unless the caller is streaming custom events,
    so a plain ``ainvoke`` in a test pays nothing for it."""
    runtime.stream_writer(event)


@dataclass(frozen=True, slots=True)
class _StreamedAnswer:
    raw_text: str
    prompt_tokens: int | None
    completion_tokens: int | None


async def _stream_answer(
    runtime: Runtime[AgentContext],
    prompt: Sequence[AnyMessage],
    sources: list[Source],
) -> _StreamedAnswer:
    """Stream the model's answer to the client as it is written.

    Every fragment passes through the citation filter first, so an invented
    marker never reaches the screen. The raw text is returned unfiltered: the
    caller validates it once more, as a whole, to produce the canonical answer
    that is saved.
    """
    citation_filter = CitationStreamFilter(sources)
    parts: list[str] = []
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def release(text: str, citations: list[Citation]) -> None:
        # Citations first: a client that receives "[S1]" in a delta already
        # knows which source S1 is, so it never renders an unresolvable marker.
        for citation in citations:
            _emit(runtime, CitationAdded(citation=citation))
        if text:
            _emit(runtime, TextDelta(text=text))

    async for chunk in runtime.context.llm.stream(
        prompt, max_output_tokens=runtime.context.limits.max_output_tokens
    ):
        parts.append(chunk.text)
        release(*citation_filter.feed(chunk.text))
        if chunk.prompt_tokens is not None:
            prompt_tokens = chunk.prompt_tokens
        if chunk.completion_tokens is not None:
            completion_tokens = chunk.completion_tokens
    release(*citation_filter.flush())

    return _StreamedAnswer("".join(parts), prompt_tokens, completion_tokens)


async def decide_search(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Choose between searching and answering directly."""
    _emit(runtime, StatusChanged(status="thinking"))
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
        telemetry.record_llm_failure(
            "transient" if isinstance(exc, LLMTransientError) else "permanent"
        )
        logger.warning("search_decision_failed", extra={"error_type": type(exc).__name__})
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
    _emit(runtime, StatusChanged(status="searching"))
    outcome = await execute_searches(
        state.get("queries", []),
        provider=runtime.context.search,
        limits=runtime.context.limits,
        emit=runtime.stream_writer,
    )

    update = AgentState(
        sources=outcome.sources,
        queries=outcome.executed_queries,
        search_iterations=outcome.searches_run,
    )
    if outcome.provider_failed:
        # Not terminal. A direct answer is worth more to the user than an error
        # page, as long as they are told it is unverified.
        logger.warning("search_unavailable", extra={"search_count": outcome.searches_run})
        update["degraded"] = True
        update["notice"] = SEARCH_UNAVAILABLE_NOTICE
    elif not outcome.sources:
        # The search worked and found nothing usable. The answer that follows
        # comes from memory, exactly as after an outage, and is flagged the
        # same way: a searched answer with no citation must not look sourced.
        update["degraded"] = True
        update["notice"] = NO_SOURCES_NOTICE
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

    _emit(runtime, StatusChanged(status="writing"))
    try:
        streamed = await _stream_answer(runtime, prompt, sources)
    except LLMError as exc:
        return AgentState(error=_llm_error(exc))

    answer, citations, discarded = extract_citations(streamed.raw_text, sources)
    degraded = is_degraded(searched=True, answer=answer, citations=citations)

    update = AgentState(
        answer=answer,
        citations=citations,
        model=runtime.context.llm.model,
        prompt_tokens=streamed.prompt_tokens,
        completion_tokens=streamed.completion_tokens,
        degraded=degraded or state.get("degraded", False),
    )
    if degraded:
        update["notice"] = UNVERIFIED_NOTICE
    if discarded:
        logger.warning("citations_invalid", extra={"count": len(discarded)})
    return update


async def answer_direct(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Answer from the model's own knowledge, with no citations at all."""
    history = trim_history(
        state.get("messages", []), max_messages=runtime.context.limits.max_context_messages
    )
    prompt = [SystemMessage(content=ANSWER_DIRECT_SYSTEM), *history]

    _emit(runtime, StatusChanged(status="writing"))
    try:
        # No sources exist here, so the filter strips every marker the model
        # may produce out of habit — on screen as well as in the saved text. A
        # citation with nothing behind it is the one thing this pipeline must
        # never render.
        streamed = await _stream_answer(runtime, prompt, [])
    except LLMError as exc:
        return AgentState(error=_llm_error(exc))

    answer, citations, discarded = extract_citations(streamed.raw_text, [])
    if discarded:
        logger.warning("uncited_markers_stripped", extra={"count": len(discarded)})

    return AgentState(
        answer=answer,
        citations=citations,
        model=runtime.context.llm.model,
        prompt_tokens=streamed.prompt_tokens,
        completion_tokens=streamed.completion_tokens,
    )


async def finalize(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Commit the answer to the transcript.

    Runs on every path, including failure, so that one node owns the shape of
    the result and callers never have to reconstruct it from partial state.
    """
    error = state.get("error")
    if error is not None:
        logger.info(
            "agent_failed",
            extra={"error_code": error["code"], "retryable": error["retryable"]},
        )
        return AgentState(messages=[])

    answer = state.get("answer", "")
    logger.info(
        "agent_completed",
        extra={
            "searched": state.get("search_decision", False),
            "search_count": state.get("search_iterations", 0),
            "citation_count": len(state.get("citations", [])),
            "degraded": state.get("degraded", False),
        },
    )
    # Appended through the reducer so the checkpointed transcript matches what
    # the next turn in this thread will see.
    return AgentState(messages=[AIMessage(content=answer)])
