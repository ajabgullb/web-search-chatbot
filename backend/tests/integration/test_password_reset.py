"""Password reset: enumeration safety, single use, and session revocation."""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from tests.fakes import RecordingEmailSender
from tests.integration.conftest import REGISTER_PASSWORD, register

pytestmark = pytest.mark.integration

NEW_PASSWORD = "an-entirely-different-passphrase"


def _token_from(outbox: RecordingEmailSender) -> str:
    """Pull the token out of the reset link, the way the frontend will.

    Matching the link rather than a bare indented line keeps this robust
    against an SMTP hop rewriting whitespace and line endings.
    """
    match = re.search(r"/reset-password\?token=(\S+)", outbox.last.body)
    assert match, f"no reset link found in email body: {outbox.last.body!r}"
    return match.group(1)


async def test_forgot_password_answer_is_identical_for_unknown_accounts(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    await register(client, email="real@example.com")
    email_outbox.sent.clear()  # registration's verification email

    known = await client.post("/api/v1/auth/forgot-password", json={"email": "real@example.com"})
    unknown = await client.post("/api/v1/auth/forgot-password", json={"email": "ghost@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    # Only the real account actually receives mail.
    assert len(email_outbox.sent) == 1


async def test_reset_updates_the_password_and_revokes_sessions(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    await register(client, email="reset@example.com")
    await client.post("/api/v1/auth/forgot-password", json={"email": "reset@example.com"})
    token = _token_from(email_outbox)

    reset = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}
    )
    assert reset.status_code == 200

    # The session that existed before the reset is gone.
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401

    # The old password no longer works; the new one does.
    old = await client.post(
        "/api/v1/auth/login",
        json={"email": "reset@example.com", "password": REGISTER_PASSWORD},
    )
    assert old.status_code == 401

    new = await client.post(
        "/api/v1/auth/login",
        json={"email": "reset@example.com", "password": NEW_PASSWORD},
    )
    assert new.status_code == 200


async def test_a_reset_token_works_only_once(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    await register(client, email="once@example.com")
    await client.post("/api/v1/auth/forgot-password", json={"email": "once@example.com"})
    token = _token_from(email_outbox)

    first = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}
    )
    second = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "password": "yet-another-passphrase"}
    )

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "invalid_token"


async def test_requesting_a_second_reset_invalidates_the_first(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    """Otherwise every click leaves another working credential in an inbox."""
    await register(client, email="twice@example.com")

    await client.post("/api/v1/auth/forgot-password", json={"email": "twice@example.com"})
    first_token = _token_from(email_outbox)
    await client.post("/api/v1/auth/forgot-password", json={"email": "twice@example.com"})
    second_token = _token_from(email_outbox)

    assert first_token != second_token
    stale = await client.post(
        "/api/v1/auth/reset-password", json={"token": first_token, "password": NEW_PASSWORD}
    )
    assert stale.status_code == 401

    fresh = await client.post(
        "/api/v1/auth/reset-password", json={"token": second_token, "password": NEW_PASSWORD}
    )
    assert fresh.status_code == 200


async def test_reset_rejects_an_unknown_token(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": "x" * 64, "password": NEW_PASSWORD},
    )

    assert response.status_code == 401


async def test_the_reset_email_body_is_never_logged(
    client: AsyncClient, email_outbox: RecordingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:
    await register(client, email="quiet@example.com")
    with caplog.at_level("DEBUG"):
        await client.post("/api/v1/auth/forgot-password", json={"email": "quiet@example.com"})

    token = _token_from(email_outbox)
    assert token not in caplog.text
