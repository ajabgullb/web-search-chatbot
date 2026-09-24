"""Graceful shutdown: SIGTERM finishes in-flight work instead of dropping it.

The first test runs a real uvicorn server on a real socket and triggers the
shutdown SIGTERM triggers. An ASGI test transport would skip exactly the part
under test: uvicorn closing its listener and waiting for open responses.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import uvicorn
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.db.session import Database
from app.main import create_app
from app.models.run import RunStatus
from app.repositories.runs import RunRepository
from tests.conftest import LIFESPAN_TIMEOUT_SECONDS
from tests.fakes import FakeLLM
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import (
    install_providers,
    parse_sse,
    post_run,
    wait_for_active_run,
)

pytestmark = pytest.mark.integration

ANSWER = "An answer that takes a moment to write. " * 5


async def stored_status(settings: Settings, run_id: uuid.UUID) -> RunStatus:
    database = Database(settings)
    try:
        async with database.open_session() as session:
            run = await RunRepository(session).get(run_id)
            assert run is not None
            return run.status
    finally:
        await database.dispose()


async def test_sigterm_lets_an_open_stream_finish(settings: Settings) -> None:
    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            lifespan="on",
            log_config=None,
            timeout_graceful_shutdown=15,
        )
    )
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(LIFESPAN_TIMEOUT_SECONDS):
        while not server.started:
            await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    # About 2 s of streaming: long enough to be mid-answer when SIGTERM lands.
    llm, _ = await install_providers(app, llm=FakeLLM(answers=[ANSWER], chunk_delay_seconds=0.04))
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        headers = auth_header(await register(client))
        conversation = await create_conversation(client, headers)
        streaming = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
        run = await wait_for_active_run(app, conversation["id"])
        while llm.chunks_sent < 3:
            await asyncio.sleep(0.02)

        # What uvicorn's SIGTERM handler does. The handler itself also records
        # the signal and re-raises it on the process once serving ends, which
        # here would kill the test runner.
        server.should_exit = True
        await asyncio.sleep(0.3)
        # The listener is closed: nothing new gets in while the stream drains.
        with pytest.raises(httpx.ConnectError):
            await httpx.AsyncClient(base_url=base_url).get("/api/v1/health/live")

        stream = parse_sse((await asyncio.wait_for(streaming, timeout=20)).text)

    await asyncio.wait_for(serving, timeout=20)
    assert stream.terminal["type"] == "message.completed"
    assert stream.streamed_text.strip() == ANSWER.strip()
    assert llm.finished_streams == 1
    assert await stored_status(settings, run.id) is RunStatus.completed


async def test_shutdown_lets_a_run_without_a_stream_finish(settings: Settings) -> None:
    """A run whose page was reloaded has no open stream for uvicorn to wait
    on; the drain window is what keeps it from being cut off."""
    settings.shutdown_drain_seconds = 5
    app = create_app(settings)
    async with LifespanManager(
        app, startup_timeout=LIFESPAN_TIMEOUT_SECONDS, shutdown_timeout=LIFESPAN_TIMEOUT_SECONDS
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            await install_providers(app, llm=FakeLLM(answers=["done"], delay_seconds=0.5))
            headers = auth_header(await register(client))
            conversation = await create_conversation(client, headers)
            streaming = asyncio.create_task(
                post_run(client, headers, conversation["id"], content="hi")
            )
            run = await wait_for_active_run(app, conversation["id"])
    # The lifespan has exited, and shutdown waited for the run.
    stream = parse_sse((await asyncio.wait_for(streaming, timeout=10)).text)
    assert stream.terminal["type"] == "message.completed"
    assert await stored_status(settings, run.id) is RunStatus.completed


async def test_a_draining_process_refuses_new_runs_and_reports_not_ready(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app)
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    app.state.run_manager.accepting = False

    refused = await post_run(client, headers, conversation["id"], content="hi")
    ready = await client.get("/api/v1/health/ready")

    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "service_unavailable"
    assert refused.headers["Retry-After"] == "5"
    assert ready.status_code == 503
    assert ready.json()["checks"]["accepting_work"] is False
