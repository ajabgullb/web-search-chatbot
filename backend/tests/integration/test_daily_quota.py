"""The daily per-user run quota.

The per-minute rate limit stops bursts; this caps what one account can spend
in a day. Counted from the runs table, so it is shared by every replica and
outlives a Redis restart.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from app.db.session import Database
from app.models.run import Run
from tests.fakes import FakeLLM
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import install_providers, parse_sse, post_run

pytestmark = pytest.mark.integration


async def _age_runs(app: FastAPI, *, by: timedelta) -> None:
    """Move every run's start time into the past, as if a day went by."""
    database: Database = app.state.database
    async with database.open_session() as session:
        await session.execute(update(Run).values(created_at=Run.created_at - by))
        await session.commit()


async def _ask(
    client: AsyncClient, headers: dict[str, str], conversation_id: str, *, key: str | None = None
) -> int:
    response = await post_run(client, headers, conversation_id, content="Hello", key=key)
    return response.status_code


async def test_runs_beyond_the_daily_limit_are_refused(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["One.", "Two.", "Three."]))
    app.state.settings.daily_run_limit_per_user = 2
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)

    assert await _ask(client, headers, conversation["id"]) == 200
    assert await _ask(client, headers, conversation["id"]) == 200
    refused = await post_run(client, headers, conversation["id"], content="Hello")

    assert refused.status_code == 429
    error = refused.json()["error"]
    assert error["code"] == "daily_quota_exceeded"
    assert error["details"]["limit"] == 2
    # The first run frees its slot about 24 hours after it started.
    retry_after = int(refused.headers["Retry-After"])
    assert 23 * 3600 < retry_after <= 24 * 3600
    assert error["details"]["retry_after_seconds"] == retry_after


async def test_the_window_rolls(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["One.", "Two."]))
    app.state.settings.daily_run_limit_per_user = 1
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    assert await _ask(client, headers, conversation["id"]) == 200
    assert await _ask(client, headers, conversation["id"]) == 429

    await _age_runs(app, by=timedelta(hours=24, minutes=1))

    assert await _ask(client, headers, conversation["id"]) == 200


async def test_the_quota_is_per_user(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["One.", "Two."]))
    app.state.settings.daily_run_limit_per_user = 1
    alice = auth_header(await register(client, email="alice@example.com"))
    bob = auth_header(await register(client, email="bob@example.com"))
    alice_chat = await create_conversation(client, alice)
    bob_chat = await create_conversation(client, bob)

    assert await _ask(client, alice, alice_chat["id"]) == 200
    assert await _ask(client, alice, alice_chat["id"]) == 429
    assert await _ask(client, bob, bob_chat["id"]) == 200


async def test_deleting_a_conversation_does_not_refund_its_runs(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["One.", "Two."]))
    app.state.settings.daily_run_limit_per_user = 1
    headers = auth_header(await register(client))
    first = await create_conversation(client, headers)
    assert await _ask(client, headers, first["id"]) == 200

    deleted = await client.delete(f"/api/v1/conversations/{first['id']}", headers=headers)
    assert deleted.status_code == 204
    second = await create_conversation(client, headers)

    assert await _ask(client, headers, second["id"]) == 429


async def test_a_replayed_request_is_answered_even_at_the_limit(
    app: FastAPI, client: AsyncClient
) -> None:
    """A client retrying a request the server already accepted must get the
    original answer back, even if that request used the last unit of quota."""
    await install_providers(app, llm=FakeLLM(answers=["Only answer."]))
    app.state.settings.daily_run_limit_per_user = 1
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    key = str(uuid.uuid4())

    assert await _ask(client, headers, conversation["id"], key=key) == 200
    replay = await post_run(client, headers, conversation["id"], content="Hello", key=key)

    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert parse_sse(replay.text).terminal["type"] == "message.completed"


async def test_the_refusal_is_counted_in_metrics(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["One."]))
    app.state.settings.daily_run_limit_per_user = 1
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    await _ask(client, headers, conversation["id"])
    await _ask(client, headers, conversation["id"])

    metrics = await client.get("/api/v1/metrics")

    assert 'rate_limit_hits_total{bucket="daily_quota"}' in metrics.text
