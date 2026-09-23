"""SSE wire framing and the live relay's teardown behaviour."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

from app.api.sse import HEARTBEAT, encode_event, live_events
from app.schemas.runs import MessageDeltaEvent, RunCancelledEvent
from app.services.run_executor import RunHandle

RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _handle() -> RunHandle:
    return RunHandle(run_id=RUN_ID, conversation_id=uuid.uuid4())


def test_an_event_is_one_frame_with_id_type_and_single_line_json() -> None:
    event = MessageDeltaEvent(sequence=7, run_id=RUN_ID, text="line one\nline two\r\n")
    frame = encode_event(event).decode()

    assert frame.endswith("\n\n")
    lines = frame.rstrip("\n").split("\n")
    assert lines[0] == "id: 7"
    assert lines[1] == "event: message.delta"
    assert lines[2].startswith("data: ")
    assert len(lines) == 3, "newlines in the payload must be escaped, not framed"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert payload == {
        "sequence": 7,
        "run_id": str(RUN_ID),
        "type": "message.delta",
        "text": "line one\nline two\r\n",
    }


async def test_a_quiet_run_produces_heartbeats() -> None:
    handle = _handle()
    stream = live_events(handle, heartbeat_seconds=0.01, on_abandoned=lambda: None)
    assert await anext(stream) == HEARTBEAT
    assert await anext(stream) == HEARTBEAT
    await stream.aclose()


async def test_the_relay_ends_at_the_end_of_stream_marker() -> None:
    handle = _handle()
    abandoned: list[bool] = []
    handle.publish(RunCancelledEvent(sequence=1, run_id=RUN_ID, reason="user_cancelled"))
    handle.close()

    frames = [
        frame
        async for frame in live_events(
            handle, heartbeat_seconds=5, on_abandoned=lambda: abandoned.append(True)
        )
    ]
    assert len(frames) == 1
    assert abandoned == [], "a run that finished was not abandoned"


async def test_closing_the_relay_early_reports_abandonment() -> None:
    """What happens when the client disconnects: Starlette cancels the body
    iterator, and the run must be told."""
    handle = _handle()
    abandoned: list[bool] = []
    stream = live_events(handle, heartbeat_seconds=5, on_abandoned=lambda: abandoned.append(True))

    consumer = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer
    await stream.aclose()

    assert abandoned == [True]
