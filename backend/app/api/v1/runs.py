"""Run endpoints: start (streaming), recover, cancel.

These routes authenticate with the bearer token, not the refresh cookie, so
they need no Origin check: a cross-site page cannot attach an in-memory token.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, get_args

from fastapi import APIRouter, Depends, Header, Request
from starlette.responses import Response

from app.api.dependencies import (
    AgentRuntimeDep,
    CurrentUserDep,
    RunManagerDep,
    RunServiceDep,
    SettingsDep,
    enforce_run_rate_limit,
)
from app.api.sse import EventStreamResponse, live_events, replayed_events
from app.models.run import RunStatus
from app.schemas.message import MessageRead
from app.schemas.runs import (
    MessageCompletedEvent,
    RunCancelledEvent,
    RunCreate,
    RunErrorRead,
    RunEvent,
    RunEventModel,
    RunFailedEvent,
    RunRead,
    RunStartedEvent,
    describe_run_error,
)
from app.services.runs import ReplayedRun, RunView

router = APIRouter(tags=["runs"])

# Printable ASCII without spaces: what every idempotency-key generator emits,
# and safe to store, log, and echo.
IDEMPOTENCY_KEY_PATTERN = r"^[\x21-\x7E]{1,255}$"


def _event_stream_schema() -> dict[str, Any]:
    """OpenAPI for the stream body: a discriminated union of the event models.

    FastAPI describes any non-JSON media type as a plain string, which would
    leave the generated client with no event types. Built from the union
    itself, so adding an event can never leave this out of date.
    """
    refs = {
        event.model_fields["type"].default: f"#/components/schemas/{event.__name__}"
        for event in get_args(RunEventModel)
    }
    return {
        "oneOf": [{"$ref": ref} for ref in refs.values()],
        "discriminator": {"propertyName": "type", "mapping": refs},
    }


_STREAM_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "A text/event-stream of run events. Each frame's `data` is one of these "
            "JSON objects; `sequence` starts at 1 and increases by one. See docs/api.md."
        ),
        "content": {"text/event-stream": {"schema": _event_stream_schema()}},
    }
}


def _run_read(view: RunView) -> RunRead:
    assistant = view.assistant_message
    return RunRead.build(
        view.run,
        assistant_message=(
            MessageRead.build(assistant.message, assistant.citations) if assistant else None
        ),
    )


def _replay_events(replay: ReplayedRun) -> list[RunEventModel]:
    """The original outcome, re-expressed as a short stream.

    A replay tells the client how the first request ended; it does not re-run
    anything, and it has no deltas — the canonical message carries the text.
    """
    run = replay.run
    started = RunStartedEvent(
        sequence=1,
        run_id=run.id,
        conversation_id=run.conversation_id,
        user_message=MessageRead.build(replay.user_message),
        replayed=True,
    )
    terminal: RunEventModel
    if run.status is RunStatus.completed and replay.assistant_message is not None:
        assistant = replay.assistant_message
        terminal = MessageCompletedEvent(
            sequence=2,
            run_id=run.id,
            message=MessageRead.build(assistant.message, assistant.citations),
            run=RunRead.build(run),
        )
    elif run.status is RunStatus.cancelled:
        terminal = RunCancelledEvent(
            sequence=2, run_id=run.id, reason=run.error_code or "user_cancelled"
        )
    else:
        code = run.error_code or "internal_error"
        terminal = RunFailedEvent(
            sequence=2,
            run_id=run.id,
            error=RunErrorRead(
                code=code,
                message=describe_run_error(code),
                retryable=run.error_retryable if run.error_retryable is not None else True,
            ),
        )
    return [started, terminal]


@router.post(
    "/conversations/{conversation_id}/runs",
    # Declared so the event schemas are published in OpenAPI and reach the
    # generated client; the handler returns a streaming Response directly, so
    # FastAPI does not try to serialize through it.
    response_model=RunEvent,
    response_class=EventStreamResponse,
    status_code=200,
    responses=_STREAM_RESPONSE,
    dependencies=[Depends(enforce_run_rate_limit)],
)
async def create_run(
    conversation_id: uuid.UUID,
    payload: RunCreate,
    request: Request,
    current_user: CurrentUserDep,
    service: RunServiceDep,
    manager: RunManagerDep,
    runtime: AgentRuntimeDep,
    settings: SettingsDep,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            pattern=IDEMPOTENCY_KEY_PATTERN,
            description="Unique per logical request; reuse it when retrying the same request.",
        ),
    ],
) -> Response:
    """Send a message (or retry the latest one) and stream the answer."""
    outcome = await service.create(
        user=current_user,
        conversation_id=conversation_id,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if isinstance(outcome, ReplayedRun):
        return EventStreamResponse(
            replayed_events(_replay_events(outcome)),
            headers={"Idempotent-Replayed": "true"},
        )

    handle = manager.start(
        run=outcome.run,
        user_message=outcome.user_message,
        runtime=runtime,
        request_id=getattr(request.state, "request_id", None),
    )

    def client_went_away() -> None:
        manager.cancel(handle.run_id, reason="client_disconnected")

    return EventStreamResponse(
        live_events(
            handle,
            heartbeat_seconds=settings.sse_heartbeat_seconds,
            on_abandoned=client_went_away,
        )
    )


@router.get("/runs/{run_id}", response_model=RunRead)
async def get_run(
    run_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: RunServiceDep,
) -> RunRead:
    """A run's current state; the recovery path when a stream is lost."""
    return _run_read(await service.get(user=current_user, run_id=run_id))


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
async def cancel_run(
    run_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: RunServiceDep,
    manager: RunManagerDep,
) -> RunRead:
    """Stop a run. Safe to repeat; a finished run is returned unchanged."""
    return _run_read(await service.cancel(user=current_user, run_id=run_id, manager=manager))
