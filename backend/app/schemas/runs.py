"""Runs: the request that starts one, its public state, and its SSE events.

The event models are the wire protocol documented in ``docs/api.md``. Each is
a flat JSON object with a ``type`` discriminator, a per-run ``sequence`` that
starts at 1 and increases by one, and the ``run_id``. They are declared as the
streaming endpoint's response model so they land in the OpenAPI schema and,
from there, in the generated TypeScript client.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.run import Run, RunStatus
from app.schemas.message import CitationRead, MessageRead

# A parsing ceiling, not the product limit. The configured
# MAX_USER_MESSAGE_CHARS is enforced by the service, where it can be changed
# without a schema change; this bound only stops an oversized body from being
# fully validated before that check runs.
CONTENT_HARD_LIMIT = 100_000


class RunCreate(BaseModel):
    """Start a run: either for a new message, or to retry the latest one.

    A retry reuses the user message already stored, so trying again after a
    failure or a stop never duplicates the question in the history.
    """

    content: str | None = Field(default=None, max_length=CONTENT_HARD_LIMIT)
    retry_message_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> RunCreate:
        if (self.content is None) == (self.retry_message_id is None):
            raise ValueError("Provide exactly one of 'content' or 'retry_message_id'.")
        return self


# Messages for stored error codes. Only codes reach the database, so every
# user-facing sentence about a failure is written here, by us.
_ERROR_MESSAGES: dict[str, str] = {
    "llm_unavailable": "The language model is unavailable. Please try again.",
    "search_unavailable": "Web search is unavailable. Please try again.",
    "run_timeout": "The assistant took too long to answer. Please try again.",
    "run_interrupted": "The answer was interrupted before it finished. Please try again.",
    "internal_error": "Something went wrong while answering. Please try again.",
}
_DEFAULT_ERROR_MESSAGE = "The answer could not be completed. Please try again."


def describe_run_error(code: str) -> str:
    return _ERROR_MESSAGES.get(code, _DEFAULT_ERROR_MESSAGE)


class RunErrorRead(BaseModel):
    code: str
    message: str
    retryable: bool


class RunUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class RunRead(BaseModel):
    """A run's canonical state — the recovery path after a lost stream."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID | None
    status: RunStatus
    # Set only when ``status`` is ``failed``.
    error: RunErrorRead | None = None
    # Set only when ``status`` is ``cancelled``: user_cancelled,
    # client_disconnected, or conversation_deleted.
    cancel_reason: str | None = None
    search_count: int
    usage: RunUsage
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    # Embedded once the run has completed, so one request recovers everything.
    assistant_message: MessageRead | None = None

    @classmethod
    def build(cls, run: Run, *, assistant_message: MessageRead | None = None) -> RunRead:
        error = None
        if run.status is RunStatus.failed:
            code = run.error_code or "internal_error"
            error = RunErrorRead(
                code=code,
                message=describe_run_error(code),
                retryable=run.error_retryable if run.error_retryable is not None else True,
            )
        return cls(
            id=run.id,
            conversation_id=run.conversation_id,
            user_message_id=run.user_message_id,
            assistant_message_id=run.assistant_message_id,
            status=run.status,
            error=error,
            cancel_reason=run.error_code if run.status is RunStatus.cancelled else None,
            search_count=run.search_count,
            usage=RunUsage(
                prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens
            ),
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            assistant_message=assistant_message,
        )


class SourceRead(BaseModel):
    """Metadata of a search result. Deliberately no snippet: progress events
    describe what was found, they do not carry retrieved page text."""

    source_id: str
    url: str
    display_url: str
    title: str
    publisher: str | None
    published_at: datetime | None


# --- SSE events ---------------------------------------------------------------


class _RunEventBase(BaseModel):
    sequence: int = Field(ge=1)
    run_id: uuid.UUID


class RunStartedEvent(_RunEventBase):
    type: Literal["run.started"] = "run.started"
    conversation_id: uuid.UUID
    # The stored user message, with its server id: what an optimistic client
    # reconciles its placeholder against.
    user_message: MessageRead
    # True when this stream is an idempotent replay of a finished run.
    replayed: bool = False


class AgentStatusEvent(_RunEventBase):
    type: Literal["agent.status"] = "agent.status"
    status: Literal["thinking", "searching", "writing"]


class SearchStartedEvent(_RunEventBase):
    type: Literal["search.started"] = "search.started"
    query: str


class SearchCompletedEvent(_RunEventBase):
    type: Literal["search.completed"] = "search.completed"
    query: str
    sources: list[SourceRead]
    failed: bool = False


class MessageDeltaEvent(_RunEventBase):
    type: Literal["message.delta"] = "message.delta"
    text: str


class CitationEvent(_RunEventBase):
    type: Literal["citation"] = "citation"
    citation: CitationRead


class MessageCompletedEvent(_RunEventBase):
    """Terminal. ``message`` is canonical: clients replace the streamed text
    with its content rather than trusting their own concatenation."""

    type: Literal["message.completed"] = "message.completed"
    message: MessageRead
    run: RunRead


class RunFailedEvent(_RunEventBase):
    """Terminal."""

    type: Literal["run.failed"] = "run.failed"
    error: RunErrorRead


class RunCancelledEvent(_RunEventBase):
    """Terminal."""

    type: Literal["run.cancelled"] = "run.cancelled"
    reason: str


RunEventModel = (
    RunStartedEvent
    | AgentStatusEvent
    | SearchStartedEvent
    | SearchCompletedEvent
    | MessageDeltaEvent
    | CitationEvent
    | MessageCompletedEvent
    | RunFailedEvent
    | RunCancelledEvent
)

RunEvent = Annotated[RunEventModel, Field(discriminator="type")]

TERMINAL_EVENT_TYPES = frozenset({"message.completed", "run.failed", "run.cancelled"})
