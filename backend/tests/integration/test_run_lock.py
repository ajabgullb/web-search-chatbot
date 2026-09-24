"""The application-level run lock, end to end.

Every test runs twice: with the in-process lock, and with the Redis lock the
lifespan builds when Redis is configured (a fakeredis server stands in, so the
real commands and release script run, and CI needs no Redis service).

The lock sits in front of the database's one-active-run index. These tests pin
down both what the lock adds — rejection before any database work, prompt
release on cancel — and that the database still decides when the lock is lost.
"""

from __future__ import annotations

import asyncio
import uuid

import fakeredis
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.config import Settings
from app.core.run_lock import RunLock
from app.main import create_app
from app.services.run_executor import RunManager
from tests.fakes import FakeLLM
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import (
    install_providers,
    messages_for,
    parse_sse,
    post_run,
    runs_for,
    wait_for_active_run,
)

pytestmark = pytest.mark.integration


@pytest.fixture(params=["memory", "redis"])
def redis_server(request: pytest.FixtureRequest) -> fakeredis.FakeServer | None:
    return fakeredis.FakeServer() if request.param == "redis" else None


@pytest.fixture
def app(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    redis_server: fakeredis.FakeServer | None,
) -> FastAPI:
    """The real lifespan, handed a fake Redis in place of a network client."""
    if redis_server is not None:
        monkeypatch.setattr(
            "app.main.build_redis_client",
            lambda _settings: fakeredis.FakeAsyncRedis(server=redis_server),
        )
    return create_app(settings)


def run_lock(app: FastAPI) -> RunLock:
    lock: RunLock = app.state.run_lock
    return lock


async def test_a_held_lock_turns_a_submission_away_before_any_database_work(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app)
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    conversation_id = uuid.UUID(conversation["id"])
    # A run being accepted right now, perhaps on another replica: it holds the
    # lock but has no committed row yet.
    elsewhere = uuid.uuid4()
    assert await run_lock(app).acquire(conversation_id, elsewhere)

    response = await post_run(client, headers, conversation_id, content="hello")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "run_in_progress"
    # The row does not exist yet, so the lock is what names the run.
    assert error["details"]["run_id"] == str(elsewhere)
    assert await messages_for(app, conversation_id) == []
    assert await runs_for(app, conversation_id) == []
    # The refused request did not disturb the holder.
    assert await run_lock(app).holder(conversation_id) == elsewhere


async def test_the_lock_is_released_when_the_run_ends(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["one", "two"]))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    conversation_id = uuid.UUID(conversation["id"])

    first = await post_run(client, headers, conversation_id, content="first")

    assert parse_sse(first.text).terminal["type"] == "message.completed"
    assert await run_lock(app).holder(conversation_id) is None
    second = await post_run(client, headers, conversation_id, content="second")
    assert second.status_code == 200


async def test_the_lock_is_held_for_the_whole_run(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=0.4))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    conversation_id = uuid.UUID(conversation["id"])

    in_flight = asyncio.create_task(post_run(client, headers, conversation_id, content="one"))
    run = await wait_for_active_run(app, conversation_id)

    assert await run_lock(app).holder(conversation_id) == run.id
    await in_flight


async def test_a_rejected_retry_gives_the_lock_back(app: FastAPI, client: AsyncClient) -> None:
    """The lock is taken before the retry target is validated; a validation
    failure must not leave the conversation locked until the lease expires."""
    await install_providers(app, llm=FakeLLM(answers=["one", "two"]))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    conversation_id = uuid.UUID(conversation["id"])

    rejected = await client.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json={"retry_message_id": str(uuid.uuid4())},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )

    assert rejected.status_code == 404
    assert await run_lock(app).holder(conversation_id) is None
    accepted = await post_run(client, headers, conversation_id, content="hello")
    assert accepted.status_code == 200


async def test_cancel_frees_the_conversation_before_a_remote_executor_notices(
    app: FastAPI, client: AsyncClient
) -> None:
    """Cancelling a run executing on another replica releases its lock at
    once, and the old executor's late release cannot free the next run's."""
    await install_providers(app, llm=FakeLLM(answers=["old", "new"], delay_seconds=0.4))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    conversation_id = uuid.UUID(conversation["id"])

    old_task = asyncio.create_task(post_run(client, headers, conversation_id, content="old"))
    old_run = await wait_for_active_run(app, conversation_id)

    # Hide the run from this process's registry, as if it ran elsewhere.
    manager: RunManager = app.state.run_manager
    hidden = manager._active.pop(old_run.id)
    cancelled = await client.post(f"/api/v1/runs/{old_run.id}/cancel", headers=headers)
    manager._active[old_run.id] = hidden

    assert cancelled.json()["status"] == "cancelled"
    assert await run_lock(app).holder(conversation_id) is None

    # The old executor is still inside its model call; the next message is
    # accepted anyway.
    new_task = asyncio.create_task(post_run(client, headers, conversation_id, content="new"))
    await asyncio.sleep(0.05)
    new_run = next(r for r in await runs_for(app, conversation_id) if r.id != old_run.id)

    await old_task  # its executor ends and releases *its* lock, by name
    assert await run_lock(app).holder(conversation_id) == new_run.id

    assert parse_sse((await new_task).text).terminal["type"] == "message.completed"


async def test_the_database_still_guards_when_the_lock_is_lost(
    app: FastAPI, client: AsyncClient
) -> None:
    """Redis restarted, or a lease expired early: the lock no longer knows
    about the running run. The one-active-run index still refuses a second."""
    await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=0.4))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    conversation_id = uuid.UUID(conversation["id"])

    in_flight = asyncio.create_task(post_run(client, headers, conversation_id, content="one"))
    run = await wait_for_active_run(app, conversation_id)
    await run_lock(app).release(conversation_id, run.id)  # the lock forgets

    blocked = await post_run(client, headers, conversation_id, content="two")

    assert blocked.status_code == 409
    assert blocked.json()["error"]["details"]["run_id"] == str(run.id)
    # The refused request rolled back its message and gave its own lease back.
    assert await run_lock(app).holder(conversation_id) is None
    await in_flight
    assert len(await runs_for(app, conversation_id)) == 1
    assert [m.content for m in await messages_for(app, conversation_id)] == ["one", "slow"]


class TestRedisSpecifics:
    async def test_redis_outage_fails_closed_with_503(
        self, app: FastAPI, client: AsyncClient, redis_server: fakeredis.FakeServer | None
    ) -> None:
        if redis_server is None:
            pytest.skip("Redis only")
        await install_providers(app)
        headers = auth_header(await register(client))
        conversation = await create_conversation(client, headers)

        redis_server.connected = False
        response = await post_run(client, headers, conversation["id"], content="hello")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
        assert await messages_for(app, conversation["id"]) == []

    async def test_readiness_reports_redis_when_it_is_configured(
        self, app: FastAPI, client: AsyncClient, redis_server: fakeredis.FakeServer | None
    ) -> None:
        healthy = await client.get("/api/v1/health/ready")
        if redis_server is None:
            # Optional in development: absent is not a reason to refuse traffic.
            assert "redis" not in healthy.json()["checks"]
            return
        assert healthy.json()["checks"]["redis"] is True

        redis_server.connected = False
        down = await client.get("/api/v1/health/ready")

        assert down.status_code == 503
        assert down.json()["checks"] == {"database": True, "accepting_work": True, "redis": False}
