"""Per-account limits on sign-in and reset emails.

Per-IP limits stop one machine. These stop many machines aimed at one
account: each test spreads its attempts across several client addresses,
which the per-IP limit alone would let through.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fakes import RecordingEmailSender
from tests.integration.conftest import REGISTER_PASSWORD, register

pytestmark = pytest.mark.integration


@asynccontextmanager
async def from_address(app: FastAPI, ip: str) -> AsyncIterator[AsyncClient]:
    """A client whose requests arrive from ``ip``. Shares the running app."""
    transport = ASGITransport(app=app, client=(ip, 40000))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


async def _login(app: FastAPI, ip: str, email: str, password: str) -> int:
    async with from_address(app, ip) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        return response.status_code


async def test_sign_in_attempts_on_one_account_are_limited_across_addresses(
    app: FastAPI, client: AsyncClient
) -> None:
    app.state.settings.rate_limit_login_per_account = 3
    await register(client, email="target@example.com")

    statuses = [
        await _login(app, f"198.51.100.{n}", "target@example.com", "wrong-password-guess")
        for n in range(1, 5)
    ]

    # Every attempt came from a fresh address, so only the account limit
    # can explain the 429.
    assert statuses == [401, 401, 401, 429]


async def test_the_limit_also_stops_the_right_password(app: FastAPI, client: AsyncClient) -> None:
    """Otherwise the 429 itself would tell a guesser when a guess was right."""
    app.state.settings.rate_limit_login_per_account = 2
    await register(client, email="target@example.com")
    for n in range(1, 3):
        await _login(app, f"198.51.100.{n}", "target@example.com", "wrong-password-guess")

    status = await _login(app, "198.51.100.9", "target@example.com", REGISTER_PASSWORD)

    assert status == 429


async def test_other_accounts_are_unaffected(app: FastAPI, client: AsyncClient) -> None:
    app.state.settings.rate_limit_login_per_account = 1
    await register(client, email="target@example.com")
    await register(client, email="bystander@example.com")
    await _login(app, "198.51.100.1", "target@example.com", "wrong-password-guess")

    assert await _login(app, "198.51.100.1", "target@example.com", "wrong-password-guess") == 429
    assert await _login(app, "198.51.100.2", "bystander@example.com", REGISTER_PASSWORD) == 200


async def test_the_limit_is_keyed_on_the_normalized_address(
    app: FastAPI, client: AsyncClient
) -> None:
    """Changing case or padding must not buy a fresh allowance."""
    app.state.settings.rate_limit_login_per_account = 2
    await register(client, email="target@example.com")
    await _login(app, "198.51.100.1", "target@example.com", "wrong-password-guess")
    await _login(app, "198.51.100.2", "TARGET@Example.com", "wrong-password-guess")

    assert await _login(app, "198.51.100.3", " Target@example.COM", "wrong-password") == 429


async def test_reset_emails_to_one_address_are_capped_silently(
    app: FastAPI, client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    """The answer never changes, so the cap cannot be probed, but the inbox
    stops filling."""
    app.state.settings.rate_limit_forgot_password_per_account = 2
    await register(client, email="target@example.com")
    email_outbox.sent.clear()

    responses = []
    for n in range(1, 5):
        async with from_address(app, f"198.51.100.{n}") as other:
            responses.append(
                await other.post(
                    "/api/v1/auth/forgot-password", json={"email": "target@example.com"}
                )
            )

    assert {r.status_code for r in responses} == {202}
    assert len({r.text for r in responses}) == 1
    assert len(email_outbox.sent) == 2
