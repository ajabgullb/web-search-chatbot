"""Health endpoint behaviour that does not need a real database."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_database, get_redis, get_run_manager


class _UnavailableDatabase:
    """Stands in for a database that cannot be reached."""

    async def check_connection(self) -> bool:
        return False


class _AcceptingRunManager:
    accepting = True


async def _client_with_failing_db(app: FastAPI) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_database] = _UnavailableDatabase
    # No lifespan runs here, so there is no Redis client: as in development
    # without REDIS_URL, where readiness does not check it.
    app.dependency_overrides[get_redis] = lambda: None
    app.dependency_overrides[get_run_manager] = _AcceptingRunManager
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


async def test_liveness_ignores_dependencies(app: FastAPI) -> None:
    """Liveness must stay green when a dependency is down, or the container
    would be restarted for an outage it cannot fix."""
    async for client in _client_with_failing_db(app):
        response = await client.get("/api/v1/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


async def test_readiness_reports_503_when_database_is_down(app: FastAPI) -> None:
    async for client in _client_with_failing_db(app):
        response = await client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "not_ready",
            "checks": {"database": False, "accepting_work": True},
        }
