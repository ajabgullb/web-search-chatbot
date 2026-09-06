"""Cross-cutting HTTP behaviour: envelope, request id, headers, origin, limits."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from tests.integration.conftest import REGISTER_PASSWORD, register

pytestmark = pytest.mark.integration


async def test_every_error_uses_the_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users/me")

    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id", "details"}


async def test_validation_errors_report_fields_without_echoing_values(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "short", "display_name": ""},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    fields = {item["field"] for item in error["details"]["fields"]}
    assert fields == {"email", "password", "display_name"}
    assert "short" not in response.text


async def test_request_id_is_echoed_when_supplied(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live", headers={"X-Request-ID": "trace-me-12345"})

    assert response.headers["X-Request-ID"] == "trace-me-12345"


async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")

    assert len(response.headers["X-Request-ID"]) == 36


async def test_security_headers_are_present(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_refresh_rejects_a_foreign_origin(client: AsyncClient) -> None:
    """Second lock behind SameSite=Lax on the cookie-authenticated route."""
    await register(client, email="origin@example.com")

    response = await client.post("/api/v1/auth/refresh", headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"


async def test_refresh_accepts_the_configured_origin(
    client: AsyncClient, settings: Settings
) -> None:
    await register(client, email="good-origin@example.com")

    response = await client.post(
        "/api/v1/auth/refresh", headers={"Origin": settings.frontend_origin}
    )

    assert response.status_code == 200


async def test_login_is_rate_limited(client: AsyncClient, settings: Settings) -> None:
    await register(client, email="flood@example.com")

    limit = settings.rate_limit_login
    for _ in range(limit):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "flood@example.com", "password": "wrong-password-entirely"},
        )

    blocked = await client.post(
        "/api/v1/auth/login",
        json={"email": "flood@example.com", "password": REGISTER_PASSWORD},
    )

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) > 0
