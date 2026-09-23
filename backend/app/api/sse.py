"""Server-Sent Events framing.

Why not FastAPI's built-in ``EventSourceResponse``? It streams from a generator
*path operation*, which commits the 200 status before the handler body runs.
Starting a run can fail in ordinary ways — 404, 409 while another run is
active, 422 for a reused idempotency key — and those must reach the client as
normal JSON error envelopes. So the handler validates and commits first, then
returns this response. It also lets the heartbeat interval be configuration
rather than a library constant.

Wire format, per event::

    id: <sequence>
    event: <type>
    data: <one line of JSON>

The same ``sequence`` and ``type`` are inside the JSON too, so a client that
only reads ``data`` (a fetch-based reader, not ``EventSource``) loses nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable

from starlette.responses import StreamingResponse

from app.schemas.runs import RunEventModel
from app.services.run_executor import RunHandle

# A comment line: ignored by every SSE parser, but it is traffic, so idle
# proxies keep the connection open.
HEARTBEAT = b": heartbeat\n\n"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # nginx buffers proxied responses by default, which would hold every event
    # until the run ends. This header turns that off per response.
    "X-Accel-Buffering": "no",
}


class EventStreamResponse(StreamingResponse):
    media_type = "text/event-stream"

    def __init__(
        self, content: AsyncIterator[bytes], *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(content, headers={**SSE_HEADERS, **(headers or {})})


def encode_event(event: RunEventModel) -> bytes:
    # model_dump_json never emits a raw newline (they are escaped inside
    # strings), so the payload is always a single valid ``data:`` line.
    return (
        f"id: {event.sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()
    )


async def live_events(
    handle: RunHandle,
    *,
    heartbeat_seconds: float,
    on_abandoned: Callable[[], None],
) -> AsyncIterator[bytes]:
    """Relay a run's events until its terminal event.

    If this generator stops before the run finished, the client went away:
    Starlette cancels the response when it sees ``http.disconnect``, and a
    failed write ends it too. Either way ``finally`` runs, and the run is
    cancelled rather than left to spend budget on an answer nobody receives.
    """
    try:
        while True:
            try:
                # Cancelling Queue.get() on timeout is safe: no item is lost.
                event = await asyncio.wait_for(handle.next_event(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield HEARTBEAT
                continue
            if event is None:
                return
            yield encode_event(event)
    finally:
        if not handle.finished:
            on_abandoned()


async def replayed_events(events: Iterable[RunEventModel]) -> AsyncIterator[bytes]:
    for event in events:
        yield encode_event(event)
