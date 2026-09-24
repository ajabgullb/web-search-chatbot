"""With Redis configured, the lifespan wires the shared limiter into routes."""

from __future__ import annotations

import fakeredis
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.config import Settings
from app.core.rate_limit import RedisRateLimiter
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def app(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, redis_server: fakeredis.FakeServer
) -> FastAPI:
    monkeypatch.setattr(
        "app.main.build_redis_client",
        lambda _settings: fakeredis.FakeAsyncRedis(server=redis_server),
    )
    return create_app(settings)


async def test_login_attempts_are_counted_in_redis(
    app: FastAPI, client: AsyncClient, settings: Settings, redis_server: fakeredis.FakeServer
) -> None:
    assert isinstance(app.state.rate_limiter, RedisRateLimiter)

    for _ in range(settings.rate_limit_login):
        await client.post(
            "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x" * 12}
        )
    blocked = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x" * 12}
    )

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    inspector = fakeredis.FakeAsyncRedis(server=redis_server)
    keys = [key async for key in inspector.scan_iter("rate-limit:login:*")]
    assert len(keys) == 1
    assert await inspector.zcard(keys[0]) == settings.rate_limit_login
