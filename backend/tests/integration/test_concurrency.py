"""One active run per conversation, enforced by the database.

The rule is a partial unique index, not a check in Python, so these tests race
real requests against it rather than mocking the check.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.db.session import Database
from app.models.message import Message, MessageRole
from app.models.run import Run, RunStatus
from tests.fakes import FakeLLM
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import (
    install_providers,
    messages_for,
    parse_sse,
    post_run,
    runs_for,
)

pytestmark = pytest.mark.integration


async def test_two_concurrent_submissions_produce_one_run_and_one_409(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["one", "two"], chunk_delay_seconds=0.05))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)

    first, second = await asyncio.gather(
        post_run(client, headers, conversation["id"], content="first question"),
        post_run(client, headers, conversation["id"], content="second question"),
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409]
    loser = first if first.status_code == 409 else second
    winner = second if loser is first else first
    error = loser.json()["error"]
    assert error["code"] == "run_in_progress"
    assert error["details"]["run_id"] == parse_sse(winner.text).events[0]["run_id"]

    # The rejected request left no trace: its user message rolled back with it.
    runs = await runs_for(app, conversation["id"])
    assert len(runs) == 1
    assert [m.role for m in await messages_for(app, conversation["id"])] == [
        MessageRole.user,
        MessageRole.assistant,
    ]


async def test_the_next_message_is_accepted_once_the_run_finishes(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["one", "two"]))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)

    assert (await post_run(client, headers, conversation["id"], content="first")).status_code == 200
    second = await post_run(client, headers, conversation["id"], content="second")

    assert second.status_code == 200
    assert parse_sse(second.text).terminal["type"] == "message.completed"
    assert [m.sequence_number for m in await messages_for(app, conversation["id"])] == [1, 2, 3, 4]


async def test_different_conversations_run_concurrently(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["a", "b"], chunk_delay_seconds=0.05))
    headers = auth_header(await register(client))
    one = await create_conversation(client, headers)
    two = await create_conversation(client, headers)

    responses = await asyncio.gather(
        post_run(client, headers, one["id"], content="hi"),
        post_run(client, headers, two["id"], content="hi"),
    )
    assert [r.status_code for r in responses] == [200, 200]


async def test_a_dead_run_does_not_lock_its_conversation_forever(
    app: FastAPI, client: AsyncClient
) -> None:
    """A process that crashed mid-run leaves a ``running`` row behind. Once it
    is older than the run deadline plus grace, the next submission clears it."""
    await install_providers(app, llm=FakeLLM(answers=["fresh answer"]))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    conversation_id = uuid.UUID(conversation["id"])

    database: Database = app.state.database
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    async with database.open_session() as session:
        me = (await client.get("/api/v1/users/me", headers=headers)).json()
        orphan_question = Message(
            conversation_id=conversation_id,
            role=MessageRole.user,
            content="asked just before the crash",
            sequence_number=1,
        )
        session.add(orphan_question)
        await session.flush()
        session.add(
            Run(
                user_id=uuid.UUID(me["id"]),
                conversation_id=conversation_id,
                user_message_id=orphan_question.id,
                status=RunStatus.running,
                idempotency_key="from-the-crashed-process",
                request_fingerprint="0" * 64,
                search_count=0,
                created_at=long_ago,
                started_at=long_ago,
            )
        )
        await session.commit()

    response = await post_run(client, headers, conversation_id, content="anyone there?")

    assert response.status_code == 200
    assert parse_sse(response.text).terminal["type"] == "message.completed"
    orphan, fresh = await runs_for(app, conversation_id)
    assert orphan.status is RunStatus.failed
    assert orphan.error_code == "run_interrupted"
    assert fresh.status is RunStatus.completed


async def test_a_recent_active_run_is_not_mistaken_for_a_dead_one(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=0.5))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)

    in_flight = asyncio.create_task(post_run(client, headers, conversation["id"], content="one"))
    await asyncio.sleep(0.15)
    blocked = await post_run(client, headers, conversation["id"], content="two")
    await in_flight

    assert blocked.status_code == 409
