"""Fixtures for tests that talk to a real PostgreSQL instance."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.session import Database
from app.models.message import MessageRole
from app.repositories.users import UserRepository
from app.services.conversations import ConversationService
from tests.fakes import RecordingEmailSender

REGISTER_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
async def clean_database(settings: Settings) -> AsyncIterator[None]:
    """Empty the auth tables around every test.

    Truncating rather than wrapping each test in a rollback, because the
    service layer commits — a test must exercise the same commit boundaries
    the application uses, not a nested transaction that hides them.
    """
    engine = create_async_engine(str(settings.database_url))
    statement = text(
        "TRUNCATE users, auth_sessions, password_reset_tokens, conversations, messages "
        "RESTART IDENTITY CASCADE"
    )
    async with engine.begin() as connection:
        await connection.execute(statement)
    try:
        yield
    finally:
        async with engine.begin() as connection:
            await connection.execute(statement)
        await engine.dispose()


@pytest.fixture
def email_outbox(app: FastAPI, client: AsyncClient) -> RecordingEmailSender:
    """Swap the email sender for a recorder, after the lifespan has built one."""
    recorder = RecordingEmailSender()
    app.state.email_sender = recorder
    return recorder


async def register(
    client: AsyncClient,
    *,
    email: str = "user@example.com",
    password: str = REGISTER_PASSWORD,
    display_name: str = "Test User",
) -> dict[str, Any]:
    """Register an account and return the token payload."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": display_name},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def auth_header(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def create_conversation(
    client: AsyncClient, headers: dict[str, str], *, title: str | None = None
) -> dict[str, Any]:
    response = await client.post("/api/v1/conversations", json={"title": title}, headers=headers)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def seed_messages(
    app: FastAPI, *, user_email: str, conversation_id: uuid.UUID, contents: list[tuple[str, str]]
) -> None:
    """Write turns straight through the service.

    Phase 4 has no endpoint that creates messages — they arrive with a run in
    Phase 6 — so tests exercise the same service call that endpoint will make
    rather than inserting rows behind the application's back.
    """
    database: Database = app.state.database
    async for session in database.session():
        user = await UserRepository(session).get_by_email(user_email)
        assert user is not None
        service = ConversationService(session)
        for role, content in contents:
            await service.append_message(
                user=user,
                conversation_id=conversation_id,
                role=MessageRole(role),
                content=content,
            )
        break


# Python's cookiejar appends ".local" to a dotless host name, so the cookie the
# server set is stored under "testserver.local". Setting one under "testserver"
# would add a *second* cookie instead of replacing it, and the request would
# carry both — which silently defeats any test that means to present a
# different token.
COOKIE_DOMAIN = "testserver.local"
COOKIE_PATH = "/api/v1/auth"


def put_refresh_cookie(client: AsyncClient, value: str) -> None:
    """Replace the refresh cookie with ``value``."""
    client.cookies.clear()
    client.cookies.set("refresh_token", value, domain=COOKIE_DOMAIN, path=COOKIE_PATH)


def read_refresh_cookie(client: AsyncClient) -> str:
    value = client.cookies.get("refresh_token")
    assert value is not None, "no refresh cookie present"
    return value
