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

from app.core.config import Environment, Settings, get_settings
from app.main import create_app

# Default to whatever DATABASE_URL the developer's .env points at, so tests
# follow the Compose stack even when its host port has been remapped.
# TEST_DATABASE_URL overrides it (CI points at its own service container).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or str(get_settings().database_url)

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
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as http_client:
            yield http_client
