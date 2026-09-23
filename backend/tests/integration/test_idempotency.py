"""Idempotency-Key on run creation.

A client that loses the response to a POST cannot know whether the run was
created. Re-sending with the same key must be safe: it returns the original
outcome and creates nothing new.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

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


async def test_a_replayed_key_returns_the_original_outcome_and_creates_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    llm, _ = await install_providers(app, llm=FakeLLM(answers=["First and only answer."]))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    key = str(uuid.uuid4())

    first = parse_sse(
        (await post_run(client, headers, conversation["id"], content="hi", key=key)).text
    )
    replay_response = await post_run(client, headers, conversation["id"], content="hi", key=key)

    assert replay_response.status_code == 200
    assert replay_response.headers["idempotent-replayed"] == "true"
    replay = parse_sse(replay_response.text)
    assert replay.types == ["run.started", "message.completed"]
    assert replay.events[0]["replayed"] is True
    assert replay.events[0]["run_id"] == first.events[0]["run_id"]
    assert replay.terminal["message"] == first.terminal["message"]

    # Nothing new: one question, one answer, one run, one model call.
    assert len(await messages_for(app, conversation["id"])) == 2
    assert len(await runs_for(app, conversation["id"])) == 1
    assert len(llm.calls) == 1


async def test_a_replay_of_a_failed_run_reports_the_failure(
    app: FastAPI, client: AsyncClient
) -> None:
    from app.providers.llm import LLMTransientError

    await install_providers(app, llm=FakeLLM(answer_error=LLMTransientError("503")))
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    key = str(uuid.uuid4())

    await post_run(client, headers, conversation["id"], content="hi", key=key)
    replay = parse_sse(
        (await post_run(client, headers, conversation["id"], content="hi", key=key)).text
    )

    assert replay.types == ["run.started", "run.failed"]
    assert replay.terminal["error"]["code"] == "llm_unavailable"
    assert len(await runs_for(app, conversation["id"])) == 1


async def test_a_key_reused_for_a_different_request_is_rejected(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app)
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    key = str(uuid.uuid4())

    await post_run(client, headers, conversation["id"], content="hi", key=key)
    response = await post_run(client, headers, conversation["id"], content="bye", key=key)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert len(await messages_for(app, conversation["id"])) == 2


async def test_whitespace_differences_are_the_same_request(
    app: FastAPI, client: AsyncClient
) -> None:
    """The fingerprint is taken over the normalized message, which is what
    was stored, so a client resending its own input byte-for-byte or with
    CRLF line endings is recognised either way."""
    await install_providers(app)
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    key = str(uuid.uuid4())

    await post_run(client, headers, conversation["id"], content="line one\nline two", key=key)
    replay = await post_run(
        client, headers, conversation["id"], content="  line one\r\nline two  ", key=key
    )
    assert replay.status_code == 200
    assert replay.headers["idempotent-replayed"] == "true"


async def test_keys_are_scoped_per_user(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["for alice", "for bob"]))
    alice = auth_header(await register(client, email="alice@example.com"))
    bob = auth_header(await register(client, email="bob@example.com"))
    alice_chat = await create_conversation(client, alice)
    bob_chat = await create_conversation(client, bob)
    shared_key = "same-key-chosen-by-two-clients"

    await post_run(client, alice, alice_chat["id"], content="hi", key=shared_key)
    response = await post_run(client, bob, bob_chat["id"], content="hi", key=shared_key)

    assert "idempotent-replayed" not in response.headers
    assert parse_sse(response.text).terminal["message"]["content"] == "for bob"


async def test_the_key_header_is_required_and_validated(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app)
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    url = f"/api/v1/conversations/{conversation['id']}/runs"

    missing = await client.post(url, json={"content": "hi"}, headers=headers)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation_failed"

    spaced = await client.post(
        url, json={"content": "hi"}, headers={**headers, "Idempotency-Key": "has a space"}
    )
    assert spaced.status_code == 422
    assert await messages_for(app, conversation["id"]) == []


async def test_simultaneous_duplicates_create_exactly_one_run(
    app: FastAPI, client: AsyncClient
) -> None:
    """The same request sent twice at once, as a flaky network retry would."""
    llm, _ = await install_providers(
        app, llm=FakeLLM(answers=["Only once."], chunk_delay_seconds=0.02)
    )
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    key = str(uuid.uuid4())

    responses = await asyncio.gather(
        *(post_run(client, headers, conversation["id"], content="hi", key=key) for _ in range(4))
    )

    fresh = [
        r for r in responses if r.status_code == 200 and "idempotent-replayed" not in r.headers
    ]
    assert len(fresh) == 1
    for response in responses:
        if response is fresh[0]:
            continue
        # Either still in flight (409, pointing at the run) or already done
        # (a replay). Never a second run.
        if response.status_code == 409:
            body = response.json()["error"]
            assert body["code"] == "run_in_progress"
            assert body["details"]["run_id"] == parse_sse(fresh[0].text).events[0]["run_id"]
        else:
            assert response.headers["idempotent-replayed"] == "true"

    assert len(await runs_for(app, conversation["id"])) == 1
    assert [m.role.value for m in await messages_for(app, conversation["id"])] == [
        "user",
        "assistant",
    ]
    assert len(llm.calls) == 1
