"""A scan of every log line a full user session writes.

PLAN.md Phase 8: "A log scan over a full run finds no prompt bodies, page
content, cookies, or tokens." This drives register → login → refresh → a
searched, cited run → logout through the real app, captures everything the
application logs as it would be written, and searches it for each secret.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.agent.routing import SearchDecision
from app.core.logging import JsonFormatter, user_ref
from tests.conftest import TEST_ORIGIN
from tests.fakes import FakeLLM, FakeSearchProvider, hit
from tests.integration.conftest import REGISTER_PASSWORD, create_conversation, register
from tests.integration.runs_support import install_providers, parse_sse, post_run

pytestmark = pytest.mark.integration

EMAIL = "hygiene-check@example.com"
QUESTION = "zebra-question-marker: what did the central bank decide today?"
PAGE_TEXT = "okapi-page-marker: the bank held rates at 5 percent."
ANSWER = "narwhal-answer-marker: rates were held [S1]."


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    """Every record, formatted exactly as production writes it."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


async def test_a_full_session_leaves_no_secrets_or_content_in_the_logs(
    app: FastAPI, client: AsyncClient, captured_logs: io.StringIO
) -> None:
    await install_providers(
        app,
        llm=FakeLLM(
            answers=[ANSWER],
            decision=SearchDecision(should_search=True, rationale="fresh", queries=["bank rates"]),
        ),
        search=FakeSearchProvider(
            default=[hit("https://news.test/rates", title="Rates", snippet=PAGE_TEXT)]
        ),
    )
    tokens = await register(client, email=EMAIL)
    login = await client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": REGISTER_PASSWORD}
    )
    refresh_cookie = client.cookies.get("refresh_token")
    refreshed = await client.post("/api/v1/auth/refresh", headers={"Origin": TEST_ORIGIN})
    access_token = refreshed.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    conversation = await create_conversation(client, headers)
    run = await post_run(client, headers, conversation["id"], content=QUESTION)
    await client.post("/api/v1/auth/logout", headers={"Origin": TEST_ORIGIN})

    assert login.status_code == 200
    stream = parse_sse(run.text)
    assert stream.terminal["type"] == "message.completed"

    output = captured_logs.getvalue()
    lines = [json.loads(line) for line in output.splitlines()]
    assert lines, "the session should have logged something"

    secrets = {
        "password": REGISTER_PASSWORD,
        "registration access token": tokens["access_token"],
        "login access token": login.json()["access_token"],
        "refreshed access token": access_token,
        "refresh cookie": refresh_cookie,
        "email": EMAIL,
        "user question": "zebra-question-marker",
        "search page text": "okapi-page-marker",
        "model answer": "narwhal-answer-marker",
        "search query": "bank rates",
    }
    for name, secret in secrets.items():
        assert secret, name
        assert secret not in output, f"{name} leaked into the logs"

    # And the lines that should be there carry the correlation fields.
    finished = next(line for line in lines if line["event"] == "run_finished")
    run_id = stream.events[0]["run_id"]
    me = await client.get("/api/v1/users/me", headers=headers)
    assert finished["run_id"] == run_id
    assert finished["conversation_id"] == conversation["id"]
    assert finished["user"] == user_ref(uuid.UUID(me.json()["id"]))
    assert finished["request_id"]
    assert finished["status"] == "message.completed"
    assert finished["search_count"] == 1
    assert {"duration_ms", "provider", "model"} <= finished.keys()

    requests = [line for line in lines if line["event"] == "http_request"]
    assert any(line["path"].endswith("/runs") and line["status"] == 200 for line in requests)
