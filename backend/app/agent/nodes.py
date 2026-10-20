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
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from app.agent.citations import CitationStreamFilter, extract_citations, is_degraded
from app.agent.context import AgentContext
from app.agent.events import (
    AgentEvent,
    CitationAdded,
    DocumentsRetrieved,
    RetrievedDocument,
    StatusChanged,
    TextDelta,
)
from app.agent.prompts import (
    ANSWER_DIRECT_SYSTEM,
    DOCUMENTS_EMPTY_NOTE,
    DOCUMENTS_UNAVAILABLE_NOTE,
    SEARCH_DECISION_SYSTEM,
    SYNTHESIZE_SYSTEM,
    attached_documents_note,
    render_sources_block,
)
from app.agent.routing import (
    SearchDecision,
    heuristic_search_decision,
    is_conversational,
    refers_to_documents,
    wants_web,
)
from app.agent.state import AgentState, Citation, RunError, Source, trim_history
from app.agent.tools.documents import retrieval_query, to_sources
from app.agent.tools.search import execute_searches, validate_queries
from app.core import telemetry
from app.providers.llm import LLMAuthError, LLMError, LLMTransientError

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
DOCUMENTS_UNAVAILABLE_NOTICE = "Your documents could not be searched for this answer."
DOCUMENTS_EMPTY_NOTICE = (
    "Your documents do not appear to cover this question, so this answer comes from the "
    "model's own knowledge and could not be verified against sources."
)
# When the documents answered and only the web part failed, the answer is
# still sourced; the notice says what it is missing rather than flagging it.
SEARCH_UNAVAILABLE_WITH_DOCUMENTS = (
    "Web search was unavailable, so this answer draws on your documents only."
)
NO_WEB_SOURCES_WITH_DOCUMENTS = (
    "Web search found nothing usable, so this answer draws on your documents only."
)


def _with_notice(state: AgentState, notice: str) -> str:
    """Add a sentence to the run's notice rather than replacing an earlier one."""
    earlier = state.get("notice", "")
    return f"{earlier} {notice}" if earlier else notice


def _earlier_questions(messages: list[AnyMessage]) -> list[str]:
    questions = [
        message.content if isinstance(message.content, str) else str(message.content)
        for message in messages
        if isinstance(message, HumanMessage)
    ]
    return questions[:-1]


def _last_user_message(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def _llm_error(exc: LLMError) -> RunError:
    telemetry.record_llm_failure("transient" if isinstance(exc, LLMTransientError) else "permanent")
    if isinstance(exc, LLMAuthError):
        # A warning, with its own event name, because nothing but an operator
        # fixes it — and because a stale key in a long-running container is
        # otherwise indistinguishable from a provider outage in the logs.
        logger.warning(
            "llm_auth_failed",
            extra={"hint": "check LLM_API_KEY, then recreate the api container"},
        )
        return RunError(
            code="llm_auth_failed",
            message="The assistant is misconfigured and cannot answer right now.",
            retryable=False,
        )
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


async def decide_retrieval(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Choose what to read: the user's documents, the web, both, or neither.

    The two are decided separately. Documents are read whenever the
    conversation has some and the message is not small talk or a task on
    text already supplied: embedding the question costs about a millionth of
    a dollar, and the relevance floor discards what does not match. The web
    keeps its heuristics-then-model rule, except that a question plainly
    about the documents does not go to the web unless it asks for it.
    """
    _emit(runtime, StatusChanged(status="thinking"))
    messages = state.get("messages", [])
    question = _last_user_message(messages)
    documents = runtime.context.ready_documents
    limit = runtime.context.limits.max_searches_per_run
    fallback_query = retrieval_query(question, _earlier_questions(messages))

    if documents and refers_to_documents(question):
        searching = wants_web(question)
        return AgentState(
            use_documents=True,
            document_query=fallback_query,
            search_decision=searching,
            search_rationale="heuristic: about the attached documents"
            + (", and asks for the web" if searching else ""),
            queries=validate_queries([question] if searching else [], limit=limit),
        )

    use_documents = bool(documents) and is_conversational(question) is None
    heuristic = heuristic_search_decision(question)

    if heuristic.should_search is not None:
        queries = [question] if heuristic.should_search else []
        return AgentState(
            use_documents=use_documents,
            document_query=fallback_query,
            search_decision=heuristic.should_search,
            search_rationale=f"heuristic: {heuristic.rationale}",
            queries=validate_queries(queries, limit=limit),
        )

    history = trim_history(messages, max_messages=runtime.context.limits.max_context_messages)
    prompt: list[AnyMessage] = [SystemMessage(content=SEARCH_DECISION_SYSTEM)]
    if documents:
        prompt.append(SystemMessage(content=attached_documents_note(documents)))
    try:
        decision = await runtime.context.llm.structured([*prompt, *history], SearchDecision)
    except LLMError as exc:
        # The classifier is not worth failing the run over. Answering directly
        # is the cheaper wrong answer: it spends no search budget, and if the
        # model is genuinely down the answer node will report it properly.
        telemetry.record_llm_failure(
            "transient" if isinstance(exc, LLMTransientError) else "permanent"
        )
        logger.warning("search_decision_failed", extra={"error_type": type(exc).__name__})
        return AgentState(
            use_documents=use_documents,
            document_query=fallback_query,
            search_decision=False,
            search_rationale="model: decision unavailable, answering directly",
            queries=[],
        )

    queries = decision.queries or ([question] if decision.should_search else [])
    rewritten = " ".join(decision.document_query.split())
    return AgentState(
        use_documents=use_documents,
        document_query=rewritten or fallback_query,
        search_decision=decision.should_search,
        search_rationale=f"model: {decision.rationale}"[:200],
        queries=validate_queries(queries, limit=limit),
    )


async def retrieve_documents(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Read the most relevant chunks of the conversation's documents."""
    context = runtime.context
    if context.documents is None:  # pragma: no cover - routing requires documents
        return AgentState(documents_empty=True)

    _emit(runtime, StatusChanged(status="reading_documents"))
    search = await context.documents.search(
        user_id=context.user_id,
        conversation_id=context.conversation_id,
        query=state.get("document_query", "") or _last_user_message(state.get("messages", [])),
        limits=context.limits,
    )
    sources = to_sources(search.chunks, retrieved_at=datetime.now(UTC))

    used: dict[str, RetrievedDocument] = {}
    for source in sources:
        key = str(source.get("document_id"))
        previous = used.get(key)
        used[key] = RetrievedDocument(
            document_id=key,
            filename=source["title"],
            chunks=(previous.chunks if previous else 0) + 1,
        )
    _emit(runtime, DocumentsRetrieved(documents=tuple(used.values()), failed=search.failed))

    update = AgentState(
        sources=sources,
        documents_empty=not search.failed and not sources,
        documents_failed=search.failed,
    )
    planned_web = state.get("search_decision", False)
    if search.failed:
        update["notice"] = _with_notice(state, DOCUMENTS_UNAVAILABLE_NOTICE)
        # Answered from memory only if the web is not coming either.
        update["degraded"] = not planned_web
    elif not sources and not planned_web:
        update["notice"] = _with_notice(state, DOCUMENTS_EMPTY_NOTICE)
        update["degraded"] = True
    return update


async def web_search(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Run the bounded searches and normalize what comes back."""
    _emit(runtime, StatusChanged(status="searching"))
    outcome = await execute_searches(
        state.get("queries", []),
        provider=runtime.context.search,
        limits=runtime.context.limits,
        emit=runtime.stream_writer,
    )

    # Appended, never replaced: document chunks retrieved before this node are
    # sources too. ``sources`` has no reducer, so a plain return would drop them.
    earlier = state.get("sources", [])
    update = AgentState(
        sources=[*earlier, *outcome.sources],
        queries=outcome.executed_queries,
        search_iterations=outcome.searches_run,
    )
    have_documents = any(source.get("kind") == "document" for source in earlier)
    if outcome.provider_failed:
        # Not terminal. A direct answer is worth more to the user than an error
        # page, as long as they are told it is unverified.
        logger.warning("search_unavailable", extra={"search_count": outcome.searches_run})
        if have_documents:
            update["notice"] = _with_notice(state, SEARCH_UNAVAILABLE_WITH_DOCUMENTS)
        else:
            update["degraded"] = True
            update["notice"] = _with_notice(state, SEARCH_UNAVAILABLE_NOTICE)
    elif not outcome.sources:
        # The search worked and found nothing usable. The answer that follows
        # comes from memory, exactly as after an outage, and is flagged the
        # same way: a searched answer with no citation must not look sourced.
        if have_documents:
            update["notice"] = _with_notice(state, NO_WEB_SOURCES_WITH_DOCUMENTS)
        else:
            update["degraded"] = True
            update["notice"] = _with_notice(state, NO_SOURCES_NOTICE)
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
        update["notice"] = _with_notice(state, UNVERIFIED_NOTICE)
    if discarded:
        logger.warning("citations_invalid", extra={"count": len(discarded)})
    return update


async def answer_direct(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Answer from the model's own knowledge, with no citations at all."""
    history = trim_history(
        state.get("messages", []), max_messages=runtime.context.limits.max_context_messages
    )
    prompt: list[AnyMessage] = [SystemMessage(content=ANSWER_DIRECT_SYSTEM)]
    if state.get("documents_failed", False):
        prompt.append(SystemMessage(content=DOCUMENTS_UNAVAILABLE_NOTE))
    elif state.get("documents_empty", False):
        prompt.append(SystemMessage(content=DOCUMENTS_EMPTY_NOTE))
    prompt.extend(history)

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
