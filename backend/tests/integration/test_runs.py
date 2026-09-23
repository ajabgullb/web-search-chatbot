"""Run lifecycle: cancel, recover, retry, ownership, and what survives a
dropped connection."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.db.session import Database
from app.main import create_app
from app.models.run import RunStatus
from app.providers.llm import LLMTransientError
from app.services.run_executor import RunManager
from tests.conftest import LIFESPAN_TIMEOUT_SECONDS
from tests.fakes import FakeLLM, FakeSearchProvider, hit
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import (
    install_providers,
    messages_for,
    parse_sse,
    post_run,
    runs_for,
    wait_for_active_run,
    wait_until,
)
from tests.integration.test_sse import assert_well_formed

pytestmark = pytest.mark.integration

LONG_ANSWER = "This is a long answer that streams slowly so there is time to act on it. " * 3


async def _user(client: AsyncClient, email: str = "user@example.com") -> dict[str, str]:
    return auth_header(await register(client, email=email))


async def _wait_for_status(
    app: FastAPI, conversation_id: str, status: RunStatus, *, within: float = 5.0
) -> None:
    async def current() -> RunStatus:
        return (await runs_for(app, conversation_id))[-1].status

    async with asyncio.timeout(within):
        while await current() is not status:
            await asyncio.sleep(0.02)


# --- recovery -----------------------------------------------------------------


async def test_get_run_is_the_recovery_path(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(
        app,
        llm=FakeLLM(answers=["Rates are 5% [S1]."]),
        search=FakeSearchProvider(default=[hit("https://a.test/1", title="Rates")]),
    )
    headers = await _user(client)
    conversation = await create_conversation(client, headers)
    stream = parse_sse(
        (await post_run(client, headers, conversation["id"], content="latest rates?")).text
    )
    run_id = stream.events[0]["run_id"]

    response = await client.get(f"/api/v1/runs/{run_id}", headers=headers)

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "completed"
    assert run["error"] is None
    assert run["search_count"] == 1
    assert run["started_at"] is not None and run["completed_at"] is not None
    assert run["assistant_message"] == stream.terminal["message"]
    assert run["assistant_message"]["citations"][0]["title"] == "Rates"


async def test_an_in_flight_run_reports_running(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=0.5))
    headers = await _user(client)
    conversation = await create_conversation(client, headers)

    task = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
    run = await wait_for_active_run(app, conversation["id"])
    polled = (await client.get(f"/api/v1/runs/{run.id}", headers=headers)).json()
    await task

    assert polled["status"] == "running"
    assert polled["assistant_message"] is None


async def test_the_conversation_names_its_active_run_for_a_reloaded_page(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=0.4))
    headers = await _user(client)
    conversation = await create_conversation(client, headers)
    url = f"/api/v1/conversations/{conversation['id']}"

    assert (await client.get(url, headers=headers)).json()["active_run_id"] is None
    task = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
    run = await wait_for_active_run(app, conversation["id"])
    during = (await client.get(url, headers=headers)).json()
    await task
    after = (await client.get(url, headers=headers)).json()

    assert during["active_run_id"] == str(run.id)
    assert after["active_run_id"] is None


# --- cancellation ---------------------------------------------------------------


async def test_cancel_stops_the_model_and_persists_cancelled(
    app: FastAPI, client: AsyncClient
) -> None:
    llm, _ = await install_providers(
        app, llm=FakeLLM(answers=[LONG_ANSWER], chunk_delay_seconds=0.02)
    )
    headers = await _user(client)
    conversation = await create_conversation(client, headers)

    task = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
    run = await wait_for_active_run(app, conversation["id"])
    await wait_until(lambda: llm.chunks_sent > 2)  # the model is mid-answer

    response = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancel_reason"] == "user_cancelled"

    stream = parse_sse((await task).text)
    assert_well_formed(stream)
    assert stream.terminal == {
        **stream.terminal,
        "type": "run.cancelled",
        "reason": "user_cancelled",
    }
    # Stopped at the source, not merely hidden: the model stream was cancelled.
    assert llm.cancelled
    assert llm.finished_streams == 0
    # A stopped response is not an answer; only the question is kept.
    assert [m.role.value for m in await messages_for(app, conversation["id"])] == ["user"]


async def test_cancel_is_idempotent_and_leaves_a_finished_run_alone(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app)
    headers = await _user(client)
    conversation = await create_conversation(client, headers)
    stream = parse_sse((await post_run(client, headers, conversation["id"], content="hi")).text)
    run_id = stream.events[0]["run_id"]

    first = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    second = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "completed"


async def test_cancel_reaches_a_run_this_process_is_not_executing(
    app: FastAPI, client: AsyncClient
) -> None:
    """A run owned by another replica (or a dead process) is cancelled through
    the database; its executor finds the row no longer active."""
    await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=0.4))
    headers = await _user(client)
    conversation = await create_conversation(client, headers)
    task = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
    run = await wait_for_active_run(app, conversation["id"])

    # Hide the run from this process's registry, as if it ran elsewhere.
    manager: RunManager = app.state.run_manager
    hidden = manager._active.pop(run.id)
    response = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=headers)
    manager._active[run.id] = hidden

    assert response.json()["status"] == "cancelled"
    stream = parse_sse((await task).text)
    # The executor finished the model call, tried to save, lost to the cancel.
    assert stream.terminal["type"] == "run.cancelled"
    assert [m.role.value for m in await messages_for(app, conversation["id"])] == ["user"]


async def test_deleting_a_conversation_cancels_its_run(app: FastAPI, client: AsyncClient) -> None:
    llm, _ = await install_providers(
        app, llm=FakeLLM(answers=[LONG_ANSWER], chunk_delay_seconds=0.02)
    )
    headers = await _user(client)
    conversation = await create_conversation(client, headers)
    task = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
    run = await wait_for_active_run(app, conversation["id"])
    await wait_until(lambda: llm.chunks_sent > 0)

    deleted = await client.delete(f"/api/v1/conversations/{conversation['id']}", headers=headers)
    stream = parse_sse((await task).text)

    assert deleted.status_code == 204
    assert stream.terminal["reason"] == "conversation_deleted"
    assert llm.cancelled
    # And the run went with the conversation from every read path.
    gone = await client.get(f"/api/v1/runs/{run.id}", headers=headers)
    assert gone.status_code == 404


# --- dropped connections --------------------------------------------------------


async def _stream_then_disconnect(
    app: FastAPI, *, path: str, headers: dict[str, str], body: dict[str, Any], after_events: int
) -> list[bytes]:
    """Drive the ASGI app directly and hang up after ``after_events`` events.

    httpx's test transport cannot disconnect mid-response, so this plays the
    server's part: it delivers ``http.disconnect`` exactly as uvicorn does
    when a browser tab closes.
    """
    chunks: list[bytes] = []
    hang_up = asyncio.Event()
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        await hang_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if b"".join(chunks).count(b"\nevent: ") >= after_events:
                hang_up.set()

    scope = {
        "type": "http",
        # uvicorn reports 2.3, under which Starlette itself listens for the
        # disconnect and cancels the response.
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in {**headers, "content-type": "application/json"}.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443),
        "state": {},
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return chunks


async def test_a_client_that_disconnects_stops_server_side_work(
    app: FastAPI, client: AsyncClient
) -> None:
    llm, _ = await install_providers(
        app, llm=FakeLLM(answers=[LONG_ANSWER], chunk_delay_seconds=0.02)
    )
    app.state.run_manager.abandon_grace_seconds = 0.2
    headers = await _user(client)
    conversation = await create_conversation(client, headers)

    await _stream_then_disconnect(
        app,
        path=f"/api/v1/conversations/{conversation['id']}/runs",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        body={"content": "hi"},
        after_events=5,  # run.started, two statuses, a couple of deltas
    )

    await _wait_for_status(app, conversation["id"], RunStatus.cancelled)
    (run,) = await runs_for(app, conversation["id"])
    assert run.error_code == "client_disconnected"
    assert llm.cancelled, "the model stream was interrupted, not left to finish"
    assert llm.finished_streams == 0
    assert [m.role.value for m in await messages_for(app, conversation["id"])] == ["user"]


async def test_a_reloaded_page_that_keeps_polling_keeps_its_answer(
    app: FastAPI, client: AsyncClient
) -> None:
    """A reload drops the stream and reconnects through GET /runs/{id}. The
    poll renews the run's lease, so the answer finishes instead of being
    cancelled as abandoned."""
    llm, _ = await install_providers(
        app, llm=FakeLLM(answers=["Worth waiting for."], chunk_delay_seconds=0.1)
    )
    app.state.run_manager.abandon_grace_seconds = 0.3
    headers = await _user(client)
    conversation = await create_conversation(client, headers)

    await _stream_then_disconnect(
        app,
        path=f"/api/v1/conversations/{conversation['id']}/runs",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        body={"content": "hi"},
        after_events=4,
    )
    (run,) = await runs_for(app, conversation["id"])

    # The reloaded page polls well inside the grace period, until it is done.
    async with asyncio.timeout(10):
        while True:
            polled = (await client.get(f"/api/v1/runs/{run.id}", headers=headers)).json()
            if polled["status"] != "running":
                break
            await asyncio.sleep(0.1)

    assert polled["status"] == "completed"
    assert polled["assistant_message"]["content"] == "Worth waiting for."
    assert not llm.cancelled


async def test_an_answer_the_model_finished_survives_a_dropped_stream(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transport dies after the model completes, while the answer is being
    saved. The save is shielded, so the answer is still retrievable."""
    await install_providers(app, llm=FakeLLM(answers=["The complete answer."]))
    headers = await _user(client)
    conversation = await create_conversation(client, headers)

    saving = asyncio.Event()
    original = RunManager._persist

    async def slow_persist(self: RunManager, *args: Any, **kwargs: Any) -> Any:
        saving.set()
        await asyncio.sleep(0.2)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(RunManager, "_persist", slow_persist)

    task = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
    await asyncio.wait_for(saving.wait(), timeout=5)
    (run,) = await runs_for(app, conversation["id"])
    # Exactly what the SSE generator does when the connection drops.
    app.state.run_manager.cancel(run.id, reason="client_disconnected")
    await task

    await _wait_for_status(app, conversation["id"], RunStatus.completed)
    recovered = (await client.get(f"/api/v1/runs/{run.id}", headers=headers)).json()
    assert recovered["assistant_message"]["content"] == "The complete answer."
    history = await client.get(
        f"/api/v1/conversations/{conversation['id']}/messages", headers=headers
    )
    assert [m["content"] for m in history.json()["items"]] == ["hi", "The complete answer."]


async def test_shutdown_fails_in_flight_runs_as_retryable(settings: Settings) -> None:
    app = create_app(settings)
    async with LifespanManager(
        app, startup_timeout=LIFESPAN_TIMEOUT_SECONDS, shutdown_timeout=LIFESPAN_TIMEOUT_SECONDS
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=5))
            headers = await _user(client)
            conversation = await create_conversation(client, headers)
            task = asyncio.create_task(post_run(client, headers, conversation["id"], content="hi"))
            run = await wait_for_active_run(app, conversation["id"])
    # The lifespan has exited: RunManager.shutdown ran before the pool closed.
    stream = parse_sse((await asyncio.wait_for(task, timeout=10)).text)
    assert stream.terminal["type"] == "run.failed"
    assert stream.terminal["error"]["code"] == "run_interrupted"
    assert stream.terminal["error"]["retryable"] is True

    database = Database(settings)
    try:
        async with database.open_session() as session:
            from app.repositories.runs import RunRepository

            stored = await RunRepository(session).get(run.id)
            assert stored is not None
            assert stored.status is RunStatus.failed
    finally:
        await database.dispose()


# --- retry ----------------------------------------------------------------------


async def test_retry_reuses_the_question_instead_of_duplicating_it(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answer_error=LLMTransientError("503")))
    headers = await _user(client)
    conversation = await create_conversation(client, headers)
    failed = parse_sse((await post_run(client, headers, conversation["id"], content="hi")).text)
    question_id = failed.events[0]["user_message"]["id"]

    await install_providers(app, llm=FakeLLM(answers=["Second time lucky."]))
    retried = await post_run(client, headers, conversation["id"], retry_message_id=question_id)

    stream = parse_sse(retried.text)
    assert_well_formed(stream)
    assert stream.events[0]["user_message"]["id"] == question_id
    assert stream.terminal["message"]["content"] == "Second time lucky."
    assert [(m.role.value, m.content) for m in await messages_for(app, conversation["id"])] == [
        ("user", "hi"),
        ("assistant", "Second time lucky."),
    ]
    first_run, second_run = await runs_for(app, conversation["id"])
    assert first_run.user_message_id == second_run.user_message_id


async def test_only_the_latest_user_message_can_be_retried(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["one", "two"]))
    headers = await _user(client)
    conversation = await create_conversation(client, headers)
    first = parse_sse((await post_run(client, headers, conversation["id"], content="q1")).text)
    await post_run(client, headers, conversation["id"], content="q2")

    stale = await post_run(
        client,
        headers,
        conversation["id"],
        retry_message_id=first.events[0]["user_message"]["id"],
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "retry_not_allowed"

    not_a_question = await post_run(
        client, headers, conversation["id"], retry_message_id=first.terminal["message"]["id"]
    )
    assert not_a_question.status_code == 404
    assert not_a_question.json()["error"]["code"] == "message_not_found"


async def test_a_follow_up_sees_earlier_turns_without_stale_citation_labels(
    app: FastAPI, client: AsyncClient
) -> None:
    llm, _ = await install_providers(
        app,
        llm=FakeLLM(answers=["Paris is the capital of France [S1].", "About two million."]),
        search=FakeSearchProvider(default=[hit("https://a.test/paris")]),
    )
    headers = await _user(client)
    conversation = await create_conversation(client, headers)
    await post_run(
        client, headers, conversation["id"], content="cite a source for France's capital"
    )
    await post_run(client, headers, conversation["id"], content="and how many people live there?")

    prompt = llm.last_prompt_text
    assert "cite a source for France's capital" in prompt
    assert "Paris is the capital of France" in prompt
    # The old [S1] named last run's source; this run's S1 (if any) is another.
    assert "[S1]" not in prompt


# --- ownership --------------------------------------------------------------------


async def test_another_users_runs_are_not_found(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app)
    alice = await _user(client, "alice@example.com")
    bob = await _user(client, "bob@example.com")
    alice_chat = await create_conversation(client, alice)
    bob_chat = await create_conversation(client, bob)
    stream = parse_sse((await post_run(client, alice, alice_chat["id"], content="hi")).text)
    run_id = stream.events[0]["run_id"]
    alice_question = stream.events[0]["user_message"]["id"]

    for response in (
        await client.get(f"/api/v1/runs/{run_id}", headers=bob),
        await client.post(f"/api/v1/runs/{run_id}/cancel", headers=bob),
        await client.get(f"/api/v1/runs/{uuid.uuid4()}", headers=bob),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "run_not_found"

    into_alices = await post_run(client, bob, alice_chat["id"], content="hi")
    assert into_alices.status_code == 404
    assert into_alices.json()["error"]["code"] == "conversation_not_found"

    # Alice's message id smuggled into Bob's own conversation.
    smuggled = await post_run(client, bob, bob_chat["id"], retry_message_id=alice_question)
    assert smuggled.status_code == 404
    assert len(await runs_for(app, alice_chat["id"])) == 1


async def test_run_routes_require_authentication(app: FastAPI, client: AsyncClient) -> None:
    run_id = uuid.uuid4()
    for response in (
        await client.post(
            f"/api/v1/conversations/{uuid.uuid4()}/runs",
            json={"content": "hi"},
            headers={"Idempotency-Key": "k"},
        ),
        await client.get(f"/api/v1/runs/{run_id}"),
        await client.post(f"/api/v1/runs/{run_id}/cancel"),
    ):
        assert response.status_code == 401


# --- input limits -----------------------------------------------------------------


async def test_message_limits_are_enforced(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app)
    app.state.settings.max_user_message_chars = 20
    headers = await _user(client)
    conversation = await create_conversation(client, headers)

    too_long = await post_run(client, headers, conversation["id"], content="x" * 21)
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "message_too_long"
    assert too_long.json()["error"]["details"] == {"max_chars": 20, "chars": 21}

    blank = await post_run(client, headers, conversation["id"], content=" \n\t ")
    assert blank.json()["error"]["code"] == "message_empty"

    both = await client.post(
        f"/api/v1/conversations/{conversation['id']}/runs",
        json={"content": "hi", "retry_message_id": str(uuid.uuid4())},
        headers={**headers, "Idempotency-Key": "k1"},
    )
    assert both.json()["error"]["code"] == "validation_failed"

    assert await messages_for(app, conversation["id"]) == []


async def test_runs_are_rate_limited_per_user(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["a", "b"]))
    app.state.settings.rate_limit_runs = 1
    headers = await _user(client)
    conversation = await create_conversation(client, headers)

    assert (await post_run(client, headers, conversation["id"], content="a")).status_code == 200
    limited = await post_run(client, headers, conversation["id"], content="b")

    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.json()["error"]["details"]["retry_after_seconds"] >= 1
