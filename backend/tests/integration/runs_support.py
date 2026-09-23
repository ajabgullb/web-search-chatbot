"""Helpers for the run and streaming tests.

httpx's ASGI transport collects a streamed body and returns it whole once the
response ends, so these tests assert on the complete event sequence. Tests
that need to act *during* a stream (cancel, disconnect) start the request as a
background task and watch the database instead.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select

from app.db.session import Database
from app.models.message import Message
from app.models.run import Run, RunStatus
from app.providers.llm import LLMProvider
from app.providers.search import SearchProvider
from tests.fakes import FakeLLM, FakeSearchProvider


@dataclass
class Frame:
    id: str | None
    event: str | None
    data: str


@dataclass
class ParsedStream:
    frames: list[Frame] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)

    @property
    def events(self) -> list[dict[str, Any]]:
        return [json.loads(frame.data) for frame in self.frames]

    @property
    def types(self) -> list[str]:
        return [event["type"] for event in self.events]

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == event_type]

    @property
    def terminal(self) -> dict[str, Any]:
        return self.events[-1]

    @property
    def streamed_text(self) -> str:
        return "".join(event["text"] for event in self.of_type("message.delta"))


def parse_sse(body: str) -> ParsedStream:
    """A strict parser: every block must be a comment or an id/event/data frame."""
    parsed = ParsedStream()
    assert body.endswith("\n\n"), "a stream must end on a frame boundary"
    for block in body.split("\n\n"):
        if not block:
            continue
        lines = block.split("\n")
        if all(line.startswith(":") for line in lines):
            parsed.comments.extend(line[1:].strip() for line in lines)
            continue
        fields: dict[str, str] = {}
        for line in lines:
            name, _, value = line.partition(": ")
            assert name in {"id", "event", "data"}, f"unexpected SSE field {name!r}"
            assert name not in fields, f"duplicate {name!r} in one frame"
            fields[name] = value
        parsed.frames.append(Frame(fields.get("id"), fields.get("event"), fields["data"]))
    return parsed


async def install_providers(
    app: FastAPI,
    *,
    llm: LLMProvider | None = None,
    search: SearchProvider | None = None,
) -> tuple[Any, Any]:
    """Swap fake providers into a running app; the next run uses them."""
    await app.state.search_provider.aclose()
    app.state.llm_provider = llm or FakeLLM()
    app.state.search_provider = search or FakeSearchProvider()
    return app.state.llm_provider, app.state.search_provider


async def post_run(
    client: AsyncClient,
    headers: dict[str, str],
    conversation_id: str | uuid.UUID,
    *,
    content: str | None = None,
    retry_message_id: str | None = None,
    key: str | None = None,
) -> Response:
    body: dict[str, Any] = {}
    if content is not None:
        body["content"] = content
    if retry_message_id is not None:
        body["retry_message_id"] = retry_message_id
    return await client.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json=body,
        headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
    )


async def runs_for(app: FastAPI, conversation_id: str | uuid.UUID) -> list[Run]:
    database: Database = app.state.database
    async with database.open_session() as session:
        result = await session.execute(
            select(Run)
            .where(Run.conversation_id == uuid.UUID(str(conversation_id)))
            .order_by(Run.created_at)
        )
        return list(result.scalars())


async def messages_for(app: FastAPI, conversation_id: str | uuid.UUID) -> list[Message]:
    database: Database = app.state.database
    async with database.open_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.conversation_id == uuid.UUID(str(conversation_id)))
            .order_by(Message.sequence_number)
        )
        return list(result.scalars())


async def wait_for_active_run(
    app: FastAPI, conversation_id: str | uuid.UUID, *, within: float = 5.0
) -> Run:
    """Poll until the conversation's run is executing, and return it.

    ``running``, not merely active: a run is briefly ``queued`` between being
    accepted and its task marking it started.
    """
    async with asyncio.timeout(within):
        while True:
            for run in await runs_for(app, conversation_id):
                if run.status is RunStatus.running and app.state.run_manager.is_active(run.id):
                    return run
            await asyncio.sleep(0.02)


async def wait_until(predicate: Any, *, within: float = 5.0) -> None:
    async with asyncio.timeout(within):
        while not predicate():
            await asyncio.sleep(0.01)
