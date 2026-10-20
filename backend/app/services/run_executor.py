"""Executing runs: the agent in a task, its events on a queue.

Why a task and not the HTTP response itself? Because the two have different
lifetimes. The response lives as long as the client's connection; the run must
live exactly as long as the work. Decoupling them gives the contract its two
guarantees at once:

- An answer the model has finished is **persisted even if the stream dies**:
  the final write is shielded from cancellation, and nothing in it depends on
  the client still listening.
- A client that goes away **stops the work** — after a short grace period.
  Nobody watching a run is not the same as nobody *ever* watching it again: a
  page reload drops the stream and reconnects a second later through the
  recovery endpoint. So a dropped stream starts an observation lease; each
  ``GET /runs/{id}`` renews it, and a run nobody renews within
  ``RUN_ABANDON_GRACE_SECONDS`` is cancelled, which interrupts the in-flight
  model stream rather than letting it bill tokens nobody will read.

Each run has one producer (its task) and one queue, so sequence numbers are
assigned in exactly one place and are strictly increasing by construction.

The registry of live runs is per process. With several replicas, a cancel that
lands on a process not executing the run is recorded in the database (see
``RunService.cancel``) and broadcast over Redis (:mod:`app.core.run_signals`),
so the owning process cancels its task at once instead of finishing a model
call nobody wants.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from app.agent.citations import strip_citation_markers
from app.agent.context import AgentContext, AgentLimits, DocumentRetriever
from app.agent.events import (
    AgentEvent,
    CitationAdded,
    DocumentsRetrieved,
    SearchCompleted,
    SearchStarted,
    StatusChanged,
    TextDelta,
)
from app.agent.graph import INTERNAL_ERROR, RUN_TIMEOUT_ERROR, AgentGraph, AgentResult, stream_agent
from app.agent.tracing import Tracing
from app.core import telemetry
from app.core.logging import bind_log_context
from app.core.run_lock import RunLock
from app.core.run_signals import CancelRequest, RedisCancelSignal
from app.db.session import Database
from app.models.citation import Citation, CitationKind
from app.models.message import Message, MessageRole, MessageStatus
from app.models.run import Run, RunStatus
from app.providers.llm import LLMProvider
from app.providers.search import SearchProvider
from app.repositories.citations import CitationRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.repositories.runs import RunRepository
from app.schemas.message import CitationRead, MessageRead
from app.schemas.runs import (
    AgentStatusEvent,
    CitationEvent,
    DocumentsRetrievedEvent,
    MessageCompletedEvent,
    MessageDeltaEvent,
    RetrievedDocumentRead,
    RunCancelledEvent,
    RunErrorRead,
    RunEventModel,
    RunFailedEvent,
    RunRead,
    RunStartedEvent,
    SearchCompletedEvent,
    SearchStartedEvent,
    SourceRead,
    describe_run_error,
)

logger = logging.getLogger(__name__)

# Cancellation reasons that mean "stopped on purpose" and are recorded as
# ``cancelled``. Anything else that stops a run (a shutdown) is a failure the
# user should be invited to retry.
CANCEL_REASONS = frozenset({"user_cancelled", "client_disconnected", "conversation_deleted"})

SHUTDOWN_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """What a run needs from the application, resolved per request so tests
    (and, later, per-user provider choices) can substitute any part."""

    graph: AgentGraph
    llm: LLMProvider
    search: SearchProvider
    limits: AgentLimits
    tracing: Tracing | None = None
    documents: DocumentRetriever | None = None


@dataclass(eq=False)
class RunHandle:
    """The live side of one run: its task and the events it has produced."""

    run_id: uuid.UUID
    conversation_id: uuid.UUID
    # Unbounded on purpose: the producer must never block on a slow reader
    # (that would stall the model stream), and a run's total output is already
    # bounded by the output-token limit.
    queue: asyncio.Queue[RunEventModel | None] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None
    cancel_reason: str | None = None
    finished: bool = False
    # When someone last showed interest: the stream dropping, or a poll of the
    # recovery endpoint. Read only once the stream has gone.
    last_observed: float = field(default_factory=time.monotonic)
    abandonment_watch: asyncio.Task[None] | None = None
    started: float = field(default_factory=time.monotonic)
    first_text_sent: bool = False
    _sequence: int = 0

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def publish(self, event: RunEventModel) -> None:
        self.queue.put_nowait(event)

    def close(self) -> None:
        if not self.finished:
            self.finished = True
            self.queue.put_nowait(None)  # end-of-stream sentinel

    async def next_event(self) -> RunEventModel | None:
        return await self.queue.get()


class RunManager:
    def __init__(
        self,
        database: Database,
        run_lock: RunLock,
        *,
        cancel_signal: RedisCancelSignal | None = None,
        abandon_grace_seconds: float = 10.0,
    ) -> None:
        self._database = database
        self._run_lock = run_lock
        # None with a single process: every run is local, so there is nobody
        # to tell.
        self._cancel_signal = cancel_signal
        # False once shutdown begins: new runs are refused (503) so a draining
        # process only finishes work, never starts it.
        self.accepting = True
        self._active: dict[uuid.UUID, RunHandle] = {}
        # Public so a test can shorten it on a running app.
        self.abandon_grace_seconds = abandon_grace_seconds

    # --- control ----------------------------------------------------------

    def start(
        self,
        *,
        run: Run,
        user_message: Message,
        runtime: AgentRuntime,
        request_id: str | None = None,
    ) -> RunHandle:
        handle = RunHandle(run_id=run.id, conversation_id=run.conversation_id)
        self._active[run.id] = handle
        # create_task copies the current context, so the request id set by the
        # middleware follows every log line the run writes.
        handle.task = asyncio.create_task(
            self._execute(handle, run, user_message, runtime, request_id),
            name=f"run-{run.id}",
        )
        return handle

    def cancel(self, run_id: uuid.UUID, *, reason: str) -> RunHandle | None:
        """Cancel a run executing in this process. None if it is not here."""
        handle = self._active.get(run_id)
        if handle is None or handle.task is None or handle.task.done():
            return None
        if handle.cancel_reason is not None:
            # Already stopping. First reason wins (a user's stop followed by
            # the tab closing is still a user stop), and a second
            # ``task.cancel()`` would land inside the shielded write that
            # records the stop, so the stream would lose its terminal event.
            return handle
        handle.cancel_reason = reason
        handle.task.cancel()
        return handle

    async def signal_cancel(
        self,
        *,
        reason: str,
        run_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> None:
        """Ask whichever process is executing the run(s) to stop them."""
        if self._cancel_signal is not None:
            await self._cancel_signal.publish(
                CancelRequest(reason=reason, run_id=run_id, conversation_id=conversation_id)
            )

    def on_cancel_signal(self, request: CancelRequest) -> None:
        """A cancel broadcast by some replica, possibly this one."""
        if request.reason not in CANCEL_REASONS:
            logger.warning("run_cancel_signal_ignored", extra={"reason": request.reason})
            return
        if request.run_id is not None:
            self.cancel(request.run_id, reason=request.reason)
        if request.conversation_id is not None:
            self.cancel_conversation(request.conversation_id, reason=request.reason)

    def release(self, run_id: uuid.UUID) -> None:
        """The run's stream has gone; cancel unless someone reattaches in time.

        Reattaching means polling ``GET /runs/{id}`` — what a reloaded page, or
        a client whose connection blipped, does next.
        """
        handle = self._active.get(run_id)
        if handle is None or handle.task is None or handle.task.done():
            return
        handle.last_observed = time.monotonic()
        if handle.abandonment_watch is None:
            handle.abandonment_watch = asyncio.create_task(
                self._watch_abandonment(handle), name=f"run-{run_id}-lease"
            )

    def observe(self, run_id: uuid.UUID) -> None:
        """Someone is still following this run: renew its lease."""
        handle = self._active.get(run_id)
        if handle is not None:
            handle.last_observed = time.monotonic()

    async def _watch_abandonment(self, handle: RunHandle) -> None:
        while not handle.finished:
            idle = time.monotonic() - handle.last_observed
            if idle >= self.abandon_grace_seconds:
                logger.info(
                    "run_abandoned", extra={"run_id": handle.run_id, "idle_seconds": round(idle, 1)}
                )
                self.cancel(handle.run_id, reason="client_disconnected")
                return
            await asyncio.sleep(self.abandon_grace_seconds - idle)

    def cancel_conversation(self, conversation_id: uuid.UUID, *, reason: str) -> None:
        for handle in list(self._active.values()):
            if handle.conversation_id == conversation_id:
                self.cancel(handle.run_id, reason=reason)

    async def wait(self, handle: RunHandle, *, grace_seconds: float) -> None:
        """Give a cancelled run a moment to record its outcome."""
        if handle.task is not None:
            await asyncio.wait({handle.task}, timeout=grace_seconds)

    def is_active(self, run_id: uuid.UUID) -> bool:
        return run_id in self._active

    async def shutdown(self, *, drain_seconds: float = 0.0) -> None:
        """Finish what is in flight if it fits in ``drain_seconds``, then stop the rest.

        A run that finishes in the window is answered and saved as usual. A run
        still going at the end is cancelled and recorded as ``run_interrupted``
        (retryable), not left ``running`` for the stale sweep to find, so its
        client learns at once that it should retry.
        """
        self.accepting = False
        in_flight = [h.task for h in self._active.values() if h.task is not None]
        if in_flight and drain_seconds > 0:
            logger.info(
                "runs_draining", extra={"count": len(in_flight), "drain_seconds": drain_seconds}
            )
            await asyncio.wait(in_flight, timeout=drain_seconds)

        stopping = []
        for handle in list(self._active.values()):
            self.cancel(handle.run_id, reason="server_shutdown")
            if handle.task is not None:
                stopping.append(handle.task)
        if stopping:
            logger.warning("runs_interrupted_by_shutdown", extra={"count": len(stopping)})
            await asyncio.wait(stopping, timeout=SHUTDOWN_GRACE_SECONDS)

    # --- execution --------------------------------------------------------

    async def _execute(
        self,
        handle: RunHandle,
        run: Run,
        user_message: Message,
        runtime: AgentRuntime,
        request_id: str | None,
    ) -> None:
        # The task's copy of the request context: every line this run writes,
        # down to the graph's nodes, carries these.
        bind_log_context(run_id=str(run.id), conversation_id=str(run.conversation_id))
        started = time.monotonic()
        handle.publish(
            RunStartedEvent(
                sequence=handle.next_sequence(),
                run_id=run.id,
                conversation_id=run.conversation_id,
                user_message=MessageRead.build(user_message),
            )
        )
        try:
            try:
                history = await self._begin(run, user_message, runtime.limits)
                if history is None:
                    # Cancelled before it started (another replica, or the
                    # stale sweep). Report whatever the database now says.
                    outcome_event = await self._current_outcome(handle, run.id)
                    handle.publish(outcome_event)
                    telemetry.record_run(outcome_event.type, time.monotonic() - started)
                    return

                # Read once, before the graph: whether there is anything to
                # search decides the route, and the filenames inform the
                # routing model.
                ready = (
                    await runtime.documents.ready_documents(
                        user_id=run.user_id, conversation_id=run.conversation_id
                    )
                    if runtime.documents is not None
                    else []
                )
                context = AgentContext(
                    user_id=run.user_id,
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    llm=runtime.llm,
                    search=runtime.search,
                    limits=runtime.limits,
                    request_id=request_id,
                    trace=runtime.tracing.for_run(run_id=run.id) if runtime.tracing else None,
                    documents=runtime.documents,
                    ready_documents=tuple(ready),
                )
                result = await self._drive(handle, runtime, context, history)
            except asyncio.CancelledError:
                reason = handle.cancel_reason or "server_shutdown"
                # Shielded: the task is being cancelled, and without the shield
                # the write that records the cancellation would be cancelled too.
                event = await asyncio.shield(self._record_stop(handle, run.id, reason))
                handle.publish(event)
                telemetry.record_run(event.type, time.monotonic() - started)
                raise
            except Exception:
                # Never a raw exception to the client; the traceback stays here.
                logger.exception("run_crashed")
                result = AgentResult(answer="", error=INTERNAL_ERROR)

            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                # Shielded for the same reason, and more importantly: the model
                # has finished, and the answer must be saved even if the client
                # is gone.
                outcome = await asyncio.shield(self._persist(handle, run, result, latency_ms))
            except Exception:
                # The database write itself failed. The run stays active until
                # the stale sweep fails it; the client is told now rather than
                # left with a stream that simply stops.
                logger.exception("run_persist_failed")
                outcome = RunFailedEvent(
                    sequence=handle.next_sequence(),
                    run_id=run.id,
                    error=RunErrorRead(
                        code="internal_error",
                        message=describe_run_error("internal_error"),
                        retryable=True,
                    ),
                )
            handle.publish(outcome)
            logger.info(
                "run_finished",
                extra={
                    "status": outcome.type,
                    "duration_ms": latency_ms,
                    "search_count": result.search_count,
                    "error_code": result.error["code"] if result.error else None,
                    "provider": run.provider,
                    "model": result.model or run.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            telemetry.record_run(
                outcome.type,
                latency_ms / 1000,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )
        finally:
            handle.close()
            self._active.pop(run.id, None)
            if handle.abandonment_watch is not None:
                handle.abandonment_watch.cancel()
            # Last, after the outcome is in the database: releasing earlier
            # would let the next run start while this one could still write.
            # Shielded because this also runs while the task is being cancelled.
            await asyncio.shield(self._run_lock.release(run.conversation_id, run.id))

    async def _drive(
        self,
        handle: RunHandle,
        runtime: AgentRuntime,
        context: AgentContext,
        history: list[AnyMessage],
    ) -> AgentResult:
        """Run the graph under the deadline, forwarding progress as it comes."""
        result: AgentResult | None = None
        try:
            async with asyncio.timeout(runtime.limits.run_timeout_seconds):
                async for item in stream_agent(runtime.graph, context=context, history=history):
                    if isinstance(item, AgentResult):
                        result = item
                    else:
                        public = self._to_public(handle, item)
                        if public.type == "message.delta" and not handle.first_text_sent:
                            handle.first_text_sent = True
                            telemetry.TIME_TO_FIRST_TOKEN.observe(time.monotonic() - handle.started)
                        handle.publish(public)
        except TimeoutError:
            logger.warning("run_timeout")
            return AgentResult(answer="", error=RUN_TIMEOUT_ERROR)
        assert result is not None  # stream_agent always ends with a result
        return result

    async def _begin(
        self, run: Run, user_message: Message, limits: AgentLimits
    ) -> list[AnyMessage] | None:
        """Mark the run running and load the history it answers, in one short
        transaction — nothing is held open across the model call."""
        async with self._database.open_session() as session:
            claimed = await RunRepository(session).mark_running(
                run_id=run.id, now=datetime.now(UTC)
            )
            if not claimed:
                await session.rollback()
                return None
            rows = await MessageRepository(session).history(
                conversation_id=run.conversation_id,
                up_to_sequence=user_message.sequence_number,
                limit=limits.max_context_messages,
            )
            await session.commit()
        return [_to_langchain(row) for row in rows]

    def _to_public(self, handle: RunHandle, event: AgentEvent) -> RunEventModel:
        """The one place internal events become wire events."""
        sequence = handle.next_sequence()
        run_id = handle.run_id
        match event:
            case StatusChanged(status=status):
                return AgentStatusEvent(sequence=sequence, run_id=run_id, status=status)
            case DocumentsRetrieved(documents=documents, failed=failed):
                return DocumentsRetrievedEvent(
                    sequence=sequence,
                    run_id=run_id,
                    documents=[
                        RetrievedDocumentRead(
                            document_id=uuid.UUID(document.document_id),
                            filename=document.filename,
                            chunk_count=document.chunks,
                        )
                        for document in documents
                    ],
                    failed=failed,
                )
            case SearchStarted(query=query):
                return SearchStartedEvent(sequence=sequence, run_id=run_id, query=query)
            case SearchCompleted(query=query, sources=sources, failed=failed):
                return SearchCompletedEvent(
                    sequence=sequence,
                    run_id=run_id,
                    query=query,
                    sources=[SourceRead.model_validate(source) for source in sources],
                    failed=failed,
                )
            case TextDelta(text=text):
                return MessageDeltaEvent(sequence=sequence, run_id=run_id, text=text)
            case CitationAdded(citation=citation):
                return CitationEvent(
                    sequence=sequence,
                    run_id=run_id,
                    citation=CitationRead.model_validate(citation),
                )

    # --- persistence ------------------------------------------------------

    async def _persist(
        self, handle: RunHandle, run: Run, result: AgentResult, latency_ms: int
    ) -> RunEventModel:
        """Record the outcome and return the terminal event describing it.

        One transaction: the assistant message, its citations, and the run's
        final status land together. The run update is conditional on the run
        still being active; if a cancellation got there first, it stands, the
        answer is discarded, and the event reports the cancellation.
        """
        now = datetime.now(UTC)
        common = {
            "completed_at": now,
            "search_count": result.search_count,
            "model": result.model or run.model,
            "prompt_version": result.prompt_version,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
        }
        async with self._database.open_session() as session:
            runs = RunRepository(session)

            if result.error is not None:
                finished = await runs.finish(
                    run_id=run.id,
                    status=RunStatus.failed,
                    error_code=result.error["code"],
                    error_retryable=result.error["retryable"],
                    **common,
                )
                await session.commit()
                if finished is None:
                    return await self._current_outcome(handle, run.id)
                return RunFailedEvent(
                    sequence=handle.next_sequence(),
                    run_id=run.id,
                    error=RunErrorRead(
                        code=result.error["code"],
                        message=result.error["message"],
                        retryable=result.error["retryable"],
                    ),
                )

            messages = MessageRepository(session)
            message = messages.add(
                Message(
                    id=uuid.uuid4(),
                    conversation_id=run.conversation_id,
                    role=MessageRole.assistant,
                    content=result.answer,
                    status=MessageStatus.complete,
                    sequence_number=await messages.next_sequence_number(
                        conversation_id=run.conversation_id
                    ),
                    model=result.model or None,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    latency_ms=latency_ms,
                    degraded=result.degraded,
                    notice=result.notice or None,
                )
            )
            citations = [
                Citation(
                    message_id=message.id,
                    ordinal=citation["ordinal"],
                    source_id=citation["source_id"],
                    kind=CitationKind(citation.get("kind", "web")),
                    url=citation["url"],
                    display_url=citation["display_url"],
                    document_id=_uuid_or_none(citation.get("document_id")),
                    document_chunk_id=_uuid_or_none(citation.get("chunk_id")),
                    page=citation.get("page"),
                    title=citation["title"],
                    publisher=citation["publisher"],
                    snippet=citation["snippet"],
                    retrieved_at=citation["retrieved_at"],
                    published_at=citation["published_at"],
                )
                for citation in result.citations
            ]
            # The message row must exist before the citations and the run
            # can reference it.
            await session.flush()
            CitationRepository(session).add_all(citations)
            await session.flush()

            finished = await runs.finish(
                run_id=run.id,
                status=RunStatus.completed,
                assistant_message_id=message.id,
                **common,
            )
            if finished is None:
                await session.rollback()
                return await self._current_outcome(handle, run.id)

            await ConversationRepository(session).touch(conversation_id=run.conversation_id, at=now)
            await session.commit()
            await session.refresh(message)

        return MessageCompletedEvent(
            sequence=handle.next_sequence(),
            run_id=run.id,
            message=MessageRead.build(message, citations),
            run=RunRead.build(finished),
        )

    async def _record_stop(
        self, handle: RunHandle, run_id: uuid.UUID, reason: str
    ) -> RunEventModel:
        now = datetime.now(UTC)
        async with self._database.open_session() as session:
            runs = RunRepository(session)
            if reason in CANCEL_REASONS:
                await runs.finish(
                    run_id=run_id, status=RunStatus.cancelled, error_code=reason, completed_at=now
                )
            else:
                await runs.finish(
                    run_id=run_id,
                    status=RunStatus.failed,
                    error_code="run_interrupted",
                    error_retryable=True,
                    completed_at=now,
                )
            await session.commit()
        logger.info("run_stopped", extra={"reason": reason})
        return await self._current_outcome(handle, run_id)

    async def _current_outcome(self, handle: RunHandle, run_id: uuid.UUID) -> RunEventModel:
        """A terminal event describing the run as the database records it."""
        async with self._database.open_session() as session:
            run = await RunRepository(session).get(run_id)
        sequence = handle.next_sequence()
        if run is not None and run.status is RunStatus.cancelled:
            return RunCancelledEvent(
                sequence=sequence, run_id=run_id, reason=run.error_code or "user_cancelled"
            )
        code = (run.error_code if run is not None else None) or "run_interrupted"
        return RunFailedEvent(
            sequence=sequence,
            run_id=run_id,
            error=RunErrorRead(code=code, message=describe_run_error(code), retryable=True),
        )


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    """Graph state holds ids as strings, to stay JSON-native in checkpoints."""
    return uuid.UUID(value) if value else None


def _to_langchain(message: Message) -> AnyMessage:
    """A stored turn as the model sees it, keyed by its database id.

    The id makes the checkpoint's copy of a turn the same turn as the row it
    came from. Old citation markers are stripped: their labels belonged to
    another run's sources.
    """
    if message.role is MessageRole.user:
        return HumanMessage(content=message.content, id=str(message.id))
    return AIMessage(content=strip_citation_markers(message.content), id=str(message.id))
