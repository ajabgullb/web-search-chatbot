"""Email verification: the emailed link, resending it, and the gate on runs.

Runs spend provider money, so an account must prove it controls its address
before it can start one. Everything else (signing in, organising
conversations) works without verification.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.config import Settings
from app.core.security import create_email_verification_token
from app.providers.email import EmailDeliveryError, EmailPayload
from tests.fakes import RecordingEmailSender
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import install_providers, parse_sse, post_run

pytestmark = pytest.mark.integration


def _token_from(outbox: RecordingEmailSender) -> str:
    match = re.search(r"/verify-email\?token=(\S+)", outbox.last.body)
    assert match, f"no verification link in email body: {outbox.last.body!r}"
    return match.group(1)


async def _me(client: AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    response = await client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


class FailingEmailSender:
    async def send(self, payload: EmailPayload) -> None:
        raise EmailDeliveryError("relay down")


# --- the link -------------------------------------------------------------------


async def test_registration_emails_a_link_that_verifies_the_account(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    tokens = await register(client, email="new@example.com", verified=False)
    headers = auth_header(tokens)

    assert tokens["user"]["is_verified"] is False
    assert email_outbox.last.to == "new@example.com"

    response = await client.post(
        "/api/v1/auth/verify-email", json={"token": _token_from(email_outbox)}
    )

    assert response.status_code == 200, response.text
    assert (await _me(client, headers))["is_verified"] is True


async def test_the_link_works_twice(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    """A second click (or a mail scanner opening it first) is not an error."""
    await register(client, verified=False)
    token = _token_from(email_outbox)

    first = await client.post("/api/v1/auth/verify-email", json={"token": token})
    second = await client.post("/api/v1/auth/verify-email", json={"token": token})

    assert first.status_code == second.status_code == 200


async def test_a_tampered_link_is_rejected(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    await register(client, verified=False)
    token = _token_from(email_outbox)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")

    response = await client.post("/api/v1/auth/verify-email", json={"token": tampered})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_a_link_for_another_address_proves_nothing(
    client: AsyncClient, settings: Settings
) -> None:
    """The token names the address it was sent to; it must match the account."""
    tokens = await register(client, email="owner@example.com", verified=False)
    forged = create_email_verification_token(
        settings, user_id=uuid.UUID(tokens["user"]["id"]), email="other@example.com"
    )

    response = await client.post("/api/v1/auth/verify-email", json={"token": forged})

    assert response.status_code == 401
    assert (await _me(client, auth_header(tokens)))["is_verified"] is False


async def test_an_access_token_cannot_verify_an_account(client: AsyncClient) -> None:
    tokens = await register(client, verified=False)

    response = await client.post(
        "/api/v1/auth/verify-email", json={"token": tokens["access_token"]}
    )

    assert response.status_code == 401


async def test_registration_succeeds_when_the_email_cannot_be_sent(
    app: FastAPI, client: AsyncClient
) -> None:
    """The account exists either way; failing would leave the client retrying
    into "email already registered"."""
    app.state.email_sender = FailingEmailSender()

    tokens = await register(client, verified=False)

    assert tokens["user"]["is_verified"] is False


# --- resending ------------------------------------------------------------------


async def test_resend_emails_a_fresh_link(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    tokens = await register(client, verified=False)
    email_outbox.sent.clear()

    response = await client.post("/api/v1/auth/resend-verification", headers=auth_header(tokens))

    assert response.status_code == 202
    assert len(email_outbox.sent) == 1
    verified = await client.post(
        "/api/v1/auth/verify-email", json={"token": _token_from(email_outbox)}
    )
    assert verified.status_code == 200


async def test_resend_to_a_verified_account_sends_nothing(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    tokens = await register(client)  # verified
    email_outbox.sent.clear()

    response = await client.post("/api/v1/auth/resend-verification", headers=auth_header(tokens))

    assert response.status_code == 202
    assert email_outbox.sent == []


async def test_resend_is_limited_per_account(
    app: FastAPI, client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    """Otherwise the app could be used to flood one inbox."""
    app.state.settings.rate_limit_resend_verification = 2
    headers = auth_header(await register(client, verified=False))

    statuses = [
        (await client.post("/api/v1/auth/resend-verification", headers=headers)).status_code
        for _ in range(3)
    ]

    assert statuses == [202, 202, 429]


async def test_resend_requires_a_session(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/resend-verification")
    assert response.status_code == 401


async def test_resend_reports_a_delivery_failure_as_unavailable(
    app: FastAPI, client: AsyncClient
) -> None:
    headers = auth_header(await register(client, verified=False))
    app.state.email_sender = FailingEmailSender()

    response = await client.post("/api/v1/auth/resend-verification", headers=headers)

    assert response.status_code == 503
    assert "relay" not in response.text, "provider detail must not reach the client"


# --- the gate -------------------------------------------------------------------


async def test_an_unverified_account_cannot_start_a_run(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app)
    headers = auth_header(await register(client, verified=False))
    conversation = await create_conversation(client, headers)

    response = await post_run(client, headers, conversation["id"], content="Hello")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_not_verified"


async def test_an_unverified_account_can_still_organise_conversations(
    client: AsyncClient,
) -> None:
    headers = auth_header(await register(client, verified=False))

    conversation = await create_conversation(client, headers, title="Plans")
    listed = await client.get("/api/v1/conversations", headers=headers)

    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()["items"]] == [conversation["id"]]


async def test_verifying_unlocks_runs_without_signing_in_again(
    app: FastAPI, client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    """The gate reads the account on every request, not a claim in the token."""
    await install_providers(app)
    headers = auth_header(await register(client, verified=False))
    conversation = await create_conversation(client, headers)
    await client.post("/api/v1/auth/verify-email", json={"token": _token_from(email_outbox)})

    response = await post_run(client, headers, conversation["id"], content="Hello")

    assert response.status_code == 200, response.text
    assert parse_sse(response.text).terminal["type"] == "message.completed"


async def test_the_gate_can_be_switched_off_outside_production(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app)
    app.state.settings.require_email_verification = False
    headers = auth_header(await register(client, verified=False))
    conversation = await create_conversation(client, headers)

    response = await post_run(client, headers, conversation["id"], content="Hello")

    assert response.status_code == 200, response.text
