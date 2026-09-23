"""Shared pytest fixtures.

Tests build their own ``Settings`` rather than reading the developer's ``.env``,
so a stray local value can never change what CI asserts.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url

from app.core.config import Environment, Settings, get_settings
from app.main import create_app


def _default_test_database_url() -> str:
    """The developer's database server, but a *separate* database on it.

    Integration tests truncate every application table around each test. Aimed
    at the database the dev stack uses, that silently deletes the accounts and
    conversations someone is clicking through in the browser. Suffixing the
    name keeps the host and port the developer already configured — including a
    remapped port — while making the destructive fixture unable to reach dev
    data.
    """
    url = make_url(str(get_settings().database_url))
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


# CI sets TEST_DATABASE_URL to its own disposable service container.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _default_test_database_url()

# asgi-lifespan's default is 5s. Startup opens the checkpointer's own
# connection and runs LangGraph's idempotent table setup, which on a loaded
# machine can take longer; a slow startup is not what any test is testing.
LIFESPAN_TIMEOUT_SECONDS = 30

TEST_ORIGIN = "http://localhost:5173"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.test,
        database_url=TEST_DATABASE_URL,  # type: ignore[arg-type]
        log_level="WARNING",
        frontend_origin=TEST_ORIGIN,
        jwt_secret="test-secret-value-that-is-long-enough-for-hs256",
        # Empty host selects the logging sender, so no test needs an SMTP
        # server; the recording double replaces it where the body matters.
        smtp_host="",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired straight to the ASGI app, lifespan included.

    Running the lifespan means the engine is built and disposed exactly as it
    is in production, instead of tests patching a database onto app.state.

    The base URL is https so that httpx's cookie jar will store and resend the
    ``Secure`` refresh cookie; over http it would silently drop it, and the
    refresh tests would fail for a reason that has nothing to do with the API.
    """
    async with LifespanManager(
        app,
        startup_timeout=LIFESPAN_TIMEOUT_SECONDS,
        shutdown_timeout=LIFESPAN_TIMEOUT_SECONDS,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as http_client:
            yield http_client
