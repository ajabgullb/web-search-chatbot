"""Cancellation across replicas, and cancelling a run that is already stopping.

The replica tests run two real application instances, each with its own
lifespan, sharing one PostgreSQL database and one (fake) Redis server: the
deployment shape the signal exists for.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fakeredis
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.run_signals import CancelRequest
from app.main import create_app
from app.services.run_executor import RunManager
from tests.conftest import LIFESPAN_TIMEOUT_SECONDS
from tests.fakes import FakeLLM
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import (
    install_providers,
    parse_sse,
    post_run,
    wait_for_active_run,
    wait_until,
)

pytestmark = pytest.mark.integration

# Long enough that, unstopped, the model would still be streaming seconds
# after the test has finished asserting.
LONG_ANSWER = "word " * 400


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def app(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, redis_server: fakeredis.FakeServer
) -> FastAPI:
    """Replica A. Replica B is built by ``other_replica`` with the same patch."""
    monkeypatch.setattr(
        "app.main.build_redis_client",
        lambda _settings: fakeredis.FakeAsyncRedis(server=redis_server),
    )
    return create_app(settings)


@asynccontextmanager
async def other_replica(settings: Settings) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    replica = create_app(settings)
    async with LifespanManager(
        replica,
        startup_timeout=LIFESPAN_TIMEOUT_SECONDS,
        shutdown_timeout=LIFESPAN_TIMEOUT_SECONDS,
    ):
        transport = ASGITransport(app=replica)
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            yield replica, client


async def subscribed(*apps: FastAPI) -> None:
    for application in apps:
        await asyncio.wait_for(application.state.cancel_signal.subscribed.wait(), timeout=5)


async def test_cancel_on_another_replica_stops_the_model_at_once(
    app: FastAPI, client: AsyncClient, settings: Settings
) -> None:
    llm, _ = await install_providers(
        app, llm=FakeLLM(answers=[LONG_ANSWER], chunk_delay_seconds=0.02)
    )
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)

    async with other_replica(settings) as (replica_b, client_b):
        await subscribed(app, replica_b)
        running = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
        run = await wait_for_active_run(app, conversation["id"])
        await wait_until(lambda: llm.chunks_sent > 2)

        # Replica B has no task for this run; only the signal can reach it.
        assert not replica_b.state.run_manager.is_active(run.id)
        response = await client_b.post(f"/api/v1/runs/{run.id}/cancel", headers=headers)

        assert response.json()["status"] == "cancelled"
        stream = parse_sse((await asyncio.wait_for(running, timeout=5)).text)

    assert stream.terminal["type"] == "run.cancelled"
    assert stream.terminal["reason"] == "user_cancelled"
    # Stopped at the source on replica A, not merely marked in the database.
    assert llm.cancelled
    assert llm.finished_streams == 0


async def test_deleting_a_conversation_on_another_replica_stops_its_run(
    app: FastAPI, client: AsyncClient, settings: Settings
) -> None:
    llm, _ = await install_providers(
        app, llm=FakeLLM(answers=[LONG_ANSWER], chunk_delay_seconds=0.02)
    )
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)

    async with other_replica(settings) as (replica_b, client_b):
        await subscribed(app, replica_b)
        running = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
        await wait_for_active_run(app, conversation["id"])
        await wait_until(lambda: llm.chunks_sent > 2)

        deleted = await client_b.delete(
            f"/api/v1/conversations/{conversation['id']}", headers=headers
        )

        assert deleted.status_code == 204
        stream = parse_sse((await asyncio.wait_for(running, timeout=5)).text)

    assert stream.terminal == {**stream.terminal, "type": "run.cancelled"}
    assert stream.terminal["reason"] == "conversation_deleted"
    assert llm.finished_streams == 0


async def test_a_signal_with_an_unknown_reason_is_ignored(
    app: FastAPI, client: AsyncClient
) -> None:
    """Only cancel reasons may stop a run through the channel; anything else
    would let a stray message fail runs with arbitrary codes."""
    await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=0.3))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    running = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
    run = await wait_for_active_run(app, conversation["id"])

    manager: RunManager = app.state.run_manager
    manager.on_cancel_signal(CancelRequest(reason="server_shutdown", run_id=run.id))

    assert parse_sse((await running).text).terminal["type"] == "message.completed"


async def test_a_second_cancel_cannot_swallow_the_terminal_event(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's stop, then the tab closing, while the stop is being recorded.
    A second task.cancel() would interrupt the write and the stream would end
    without saying how the run ended."""
    await install_providers(app, llm=FakeLLM(answers=[LONG_ANSWER], chunk_delay_seconds=0.02))
    original = RunManager._record_stop

    async def slow_record_stop(self: RunManager, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(RunManager, "_record_stop", slow_record_stop)
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    running = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
    run = await wait_for_active_run(app, conversation["id"])
    manager: RunManager = app.state.run_manager

    manager.cancel(run.id, reason="user_cancelled")
    await asyncio.sleep(0.05)  # now inside the shielded write
    manager.cancel(run.id, reason="client_disconnected")

    stream = parse_sse((await running).text)
    assert stream.terminal["type"] == "run.cancelled"
    assert stream.terminal["reason"] == "user_cancelled"


class TestCancelRequestWireFormat:
    def test_round_trips(self) -> None:
        import uuid

        request = CancelRequest(reason="user_cancelled", run_id=uuid.uuid4())
        assert CancelRequest.decode(request.encode()) == request

    @pytest.mark.parametrize(
        "raw",
        [
            b"not json",
            b"[]",
            b'{"reason": "user_cancelled"}',
            b'{"reason": "user_cancelled", "run_id": "not-a-uuid"}',
            b'{"run_id": "00000000-0000-0000-0000-000000000001"}',
        ],
    )
    def test_malformed_messages_decode_to_none(self, raw: bytes) -> None:
        assert CancelRequest.decode(raw) is None
