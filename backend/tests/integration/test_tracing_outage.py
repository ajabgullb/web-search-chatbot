"""With tracing on and LangSmith unreachable, the product still works."""

from __future__ import annotations

import asyncio
import time

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import LIFESPAN_TIMEOUT_SECONDS
from tests.fakes import FakeLLM
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import install_providers, parse_sse, post_run

pytestmark = pytest.mark.integration


async def test_an_unreachable_langsmith_affects_neither_runs_nor_shutdown(
    settings: Settings,
) -> None:
    settings.langsmith_tracing = True
    settings.langsmith_api_key = "lsv2_not_a_real_key"
    settings.langsmith_endpoint = "http://127.0.0.1:9"  # nothing listens here
    app = create_app(settings)

    async with LifespanManager(
        app, startup_timeout=LIFESPAN_TIMEOUT_SECONDS, shutdown_timeout=LIFESPAN_TIMEOUT_SECONDS
    ):
        assert app.state.tracing.enabled
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            await install_providers(app, llm=FakeLLM(answers=["Still answered."]))
            headers = auth_header(await register(client))
            conversation = await create_conversation(client, headers)
            started = time.monotonic()
            response = await asyncio.wait_for(
                post_run(client, headers, conversation["id"], content="hi"), timeout=10
            )
            run_seconds = time.monotonic() - started
        shutdown_started = time.monotonic()
    shutdown_seconds = time.monotonic() - shutdown_started

    assert parse_sse(response.text).terminal["type"] == "message.completed"
    # Uploads happen off the request path: a dead endpoint adds no latency.
    assert run_seconds < 5
    # Shutdown tries to flush, but gives up on LangSmith rather than hang.
    assert shutdown_seconds < 10
