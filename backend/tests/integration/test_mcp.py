"""The MCP endpoint (ADR-0019): who gets in, what each tool may reach, and
that ``ask`` is a run like any other."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.agent.routing import SearchDecision
from app.core.config import Settings
from app.main import create_app
from tests.conftest import TEST_ORIGIN
from tests.fakes import FakeEmbeddings, FakeLLM, FakeSearchProvider, hit
from tests.integration.conftest import (
    auth_header,
    create_conversation,
    register,
    seed_messages,
)
from tests.integration.mcp_support import (
    MCP_URL,
    call_tool,
    create_token,
    mcp_post,
    mcp_request,
)
from tests.integration.runs_support import install_providers, messages_for, runs_for
from tests.pdfs import make_pdf

pytestmark = pytest.mark.integration

CITED = FakeLLM(
    answers=["Rates were held at 5% [S1]."],
    decision=SearchDecision(should_search=True, rationale="fresh", queries=["bank rates"]),
)
SEARCH = FakeSearchProvider(
    default=[hit("https://news.test/rates", title="Rates", snippet="The bank held rates.")]
)


async def _account(client: AsyncClient, email: str = "user@example.com", **token: Any) -> str:
    headers = auth_header(await register(client, email=email))
    return str((await create_token(client, headers, **token))["token"])


# --- getting in ---------------------------------------------------------------


async def test_a_request_without_a_token_is_challenged(client: AsyncClient) -> None:
    reply = await mcp_request(client, None, "tools/list", {})

    assert reply.status == 401
    assert reply.response.headers["www-authenticate"] == 'Bearer realm="mcp"'
    assert reply.response.json()["error"]["code"] == "authentication_required"


async def test_every_wrong_token_gets_the_same_answer(client: AsyncClient) -> None:
    tokens = await register(client)
    jwt = tokens["access_token"]

    for token in ["wsc_pat_not-a-real-token", jwt, "garbage", "wsc_pat_" + "a" * 200]:
        reply = await mcp_request(client, token, "tools/list", {})
        assert reply.status == 401, token
        assert reply.response.headers["www-authenticate"] == (
            'Bearer realm="mcp", error="invalid_token"'
        )


async def test_a_foreign_origin_is_refused(client: AsyncClient) -> None:
    token = await _account(client)

    foreign = await mcp_request(
        client, token, "tools/list", {}, headers={"Origin": "https://evil.example"}
    )
    own = await mcp_request(client, token, "tools/list", {}, headers={"Origin": TEST_ORIGIN})

    assert foreign.status == 403
    assert foreign.response.json()["error"]["code"] == "origin_not_allowed"
    assert own.status == 200


async def test_requests_are_rate_limited_per_token(app: FastAPI, client: AsyncClient) -> None:
    app.state.settings.rate_limit_mcp_requests = 2
    token = await _account(client, "a@example.com")
    other = await _account(client, "b@example.com")

    statuses = [(await mcp_request(client, token, "tools/list", {})).status for _ in range(3)]
    limited = await mcp_request(client, token, "tools/list", {})

    assert statuses == [200, 200, 429]
    assert int(limited.response.headers["retry-after"]) >= 1
    # Another token has its own budget.
    assert (await mcp_request(client, other, "tools/list", {})).status == 200


async def test_failed_token_checks_are_rate_limited_per_client(
    app: FastAPI, client: AsyncClient
) -> None:
    app.state.settings.rate_limit_mcp_auth_failures = 2

    statuses = [
        (await mcp_request(client, "wsc_pat_wrong", "tools/list", {})).status for _ in range(3)
    ]

    assert statuses == [401, 401, 429]


async def test_the_endpoint_can_be_switched_off(settings: Settings) -> None:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport

    app = create_app(settings.model_copy(update={"mcp_enabled": False}))
    async with (
        LifespanManager(app, startup_timeout=30, shutdown_timeout=30),
        AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client,
    ):
        assert (await client.post(MCP_URL, json={})).status_code == 404


# --- the protocol ---------------------------------------------------------------


async def test_initialize_describes_the_server_and_its_rule_for_untrusted_text(
    client: AsyncClient,
) -> None:
    token = await _account(client)

    reply = await mcp_request(
        client,
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )

    result = reply.result
    assert result["serverInfo"]["name"] == "web-search-chatbot"
    assert "untrusted_" in result["instructions"]
    assert {"tools", "resources"} <= result["capabilities"].keys()


async def test_every_tool_publishes_input_and_output_schemas(client: AsyncClient) -> None:
    token = await _account(client)

    tools = {
        tool["name"]: tool
        for tool in (await mcp_request(client, token, "tools/list")).result["tools"]
    }

    assert set(tools) == {"ask", "list_conversations", "get_messages", "search_documents"}
    for tool in tools.values():
        assert tool["outputSchema"]["type"] == "object"
        assert tool["description"]
    assert tools["ask"]["annotations"]["readOnlyHint"] is False
    assert tools["list_conversations"]["annotations"]["readOnlyHint"] is True
    assert tools["ask"]["inputSchema"]["properties"]["question"]["maxLength"] == 16_000


async def test_a_stateless_request_works_on_its_own(client: AsyncClient) -> None:
    """No initialize first: what a second replica sees mid-conversation."""
    token = await _account(client)

    reply = await call_tool(client, token, "list_conversations")

    assert reply.structured == {"conversations": [], "next_cursor": None}
    assert "mcp-session-id" not in reply.response.headers


# --- scopes and ownership ---------------------------------------------------------


async def test_a_token_can_do_only_what_its_scopes_allow(client: AsyncClient) -> None:
    headers = auth_header(await register(client))
    reader = (await create_token(client, headers, scopes=("conversations:read",)))["token"]
    asker = (await create_token(client, headers, scopes=("runs:create",)))["token"]
    conversation = await create_conversation(client, headers)

    cannot_ask = await call_tool(client, reader, "ask", {"question": "Hi"})
    cannot_read = await call_tool(client, asker, "list_conversations")
    cannot_follow_up = await call_tool(
        client, asker, "ask", {"question": "Hi", "conversation_id": conversation["id"]}
    )

    assert "(code: insufficient_scope)" in cannot_ask.tool_error
    assert "runs:create" in cannot_ask.tool_error
    assert "(code: insufficient_scope)" in cannot_read.tool_error
    # Asking into a conversation reads its history, so it needs both.
    assert "conversations:read" in cannot_follow_up.tool_error


async def test_tools_see_only_the_token_owners_conversations(
    app: FastAPI, client: AsyncClient
) -> None:
    alice_headers = auth_header(await register(client, email="alice@example.com"))
    alice = (await create_token(client, alice_headers))["token"]
    bob = await _account(client, "bob@example.com")
    conversation = await create_conversation(client, alice_headers, title="Alice's plans")
    await seed_messages(
        app,
        user_email="alice@example.com",
        conversation_id=uuid.UUID(conversation["id"]),
        contents=[("user", "A private question"), ("assistant", "A private answer")],
    )

    alices_list = await call_tool(client, alice, "list_conversations")
    bobs_list = await call_tool(client, bob, "list_conversations")
    alices_read = await call_tool(
        client, alice, "get_messages", {"conversation_id": conversation["id"]}
    )
    bobs_read = await call_tool(
        client, bob, "get_messages", {"conversation_id": conversation["id"]}
    )
    bobs_search = await call_tool(
        client, bob, "search_documents", {"conversation_id": conversation["id"], "query": "x"}
    )
    bobs_follow_up = await call_tool(
        client, bob, "ask", {"question": "And?", "conversation_id": conversation["id"]}
    )

    assert [c["title"] for c in alices_list.structured["conversations"]] == ["Alice's plans"]
    assert bobs_list.structured["conversations"] == []
    assert [m["content"] for m in alices_read.structured["messages"]] == [
        "A private question",
        "A private answer",
    ]
    for refused in (bobs_read, bobs_search, bobs_follow_up):
        assert "(code: conversation_not_found)" in refused.tool_error
    assert await runs_for(app, conversation["id"]) == []


async def test_messages_page_backwards_from_the_newest(app: FastAPI, client: AsyncClient) -> None:
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]
    conversation = await create_conversation(client, headers)
    await seed_messages(
        app,
        user_email="user@example.com",
        conversation_id=uuid.UUID(conversation["id"]),
        contents=[("user", f"message {n}") for n in range(1, 6)],
    )

    latest = await call_tool(
        client, token, "get_messages", {"conversation_id": conversation["id"], "limit": 2}
    )
    earlier = await call_tool(
        client,
        token,
        "get_messages",
        {
            "conversation_id": conversation["id"],
            "limit": 2,
            "before": latest.structured["next_cursor"],
        },
    )

    assert [m["content"] for m in latest.structured["messages"]] == ["message 4", "message 5"]
    assert [m["content"] for m in earlier.structured["messages"]] == ["message 2", "message 3"]


async def test_invalid_arguments_are_a_tool_error_not_a_crash(client: AsyncClient) -> None:
    token = await _account(client)

    bad_id = await call_tool(client, token, "get_messages", {"conversation_id": "not-a-uuid"})
    bad_cursor = await call_tool(client, token, "list_conversations", {"cursor": "%%%"})

    assert bad_id.result["isError"] is True
    assert "(code: invalid_cursor)" in bad_cursor.tool_error


# --- ask ---------------------------------------------------------------------------


async def test_ask_answers_with_sources_and_saves_the_exchange(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=CITED, search=SEARCH)
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]

    reply = await call_tool(
        client, token, "ask", {"question": "What did the bank decide?"}, progress_token="p1"
    )

    result = reply.structured
    assert result["answer"] == "Rates were held at 5% [S1]."
    assert result["sources"] == [
        {
            "label": "S1",
            "kind": "web",
            "title": "Rates",
            "url": "https://news.test/rates",
            "page": None,
            "untrusted_excerpt": "The bank held rates.",
        }
    ]
    assert result["degraded"] is False

    # Progress went to the client as it happened, in order.
    progress = [n["params"] for n in reply.notifications("notifications/progress")]
    assert progress, "ask should report progress when asked to"
    assert all(p["progressToken"] == "p1" for p in progress)
    assert [p["progress"] for p in progress] == list(range(1, len(progress) + 1))
    assert "Searching the web for: bank rates" in [p.get("message") for p in progress]

    # A new conversation, visible in the app like any other.
    conversations = (await client.get("/api/v1/conversations", headers=headers)).json()["items"]
    assert [c["id"] for c in conversations] == [result["conversation_id"]]
    assert conversations[0]["title"] == "What did the bank decide?"
    stored = await messages_for(app, result["conversation_id"])
    assert [m.content for m in stored] == ["What did the bank decide?", result["answer"]]
    [run] = await runs_for(app, result["conversation_id"])
    assert str(run.id) == result["run_id"]
    assert run.idempotency_key.startswith("mcp-")


async def test_a_follow_up_continues_the_same_conversation(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["First.", "Second."]))
    token = await _account(client)

    first = await call_tool(client, token, "ask", {"question": "One?"})
    second = await call_tool(
        client,
        token,
        "ask",
        {"question": "Two?", "conversation_id": first.structured["conversation_id"]},
    )

    assert second.structured["conversation_id"] == first.structured["conversation_id"]
    stored = await messages_for(app, first.structured["conversation_id"])
    assert [m.content for m in stored] == ["One?", "First.", "Two?", "Second."]


async def test_ask_is_refused_for_an_unverified_account_without_leaving_a_thread(
    app: FastAPI, client: AsyncClient
) -> None:
    headers = auth_header(await register(client, verified=False))
    token = (await create_token(client, headers))["token"]

    reply = await call_tool(client, token, "ask", {"question": "Hi?"})

    assert "(code: email_not_verified)" in reply.tool_error
    assert (await client.get("/api/v1/conversations", headers=headers)).json()["items"] == []


async def test_ask_counts_against_the_daily_allowance_and_cleans_up(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["One."]))
    app.state.settings.daily_run_limit_per_user = 1
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]

    first = await call_tool(client, token, "ask", {"question": "One?"})
    second = await call_tool(client, token, "ask", {"question": "Two?"})

    assert first.structured["answer"] == "One."
    assert "(code: daily_quota_exceeded)" in second.tool_error
    assert "Try again in" in second.tool_error
    # The refused question left no empty conversation behind.
    conversations = (await client.get("/api/v1/conversations", headers=headers)).json()["items"]
    assert [c["id"] for c in conversations] == [first.structured["conversation_id"]]


async def test_ask_shares_the_browsers_per_minute_run_limit(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["A.", "B."]))
    app.state.settings.rate_limit_runs = 1
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]
    conversation = await create_conversation(client, headers)

    from_browser = await client.post(
        f"/api/v1/conversations/{conversation['id']}/runs",
        json={"content": "From the browser"},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    from_mcp = await call_tool(client, token, "ask", {"question": "From MCP"})

    assert from_browser.status_code == 200
    assert "(code: rate_limited)" in from_mcp.tool_error


async def test_ask_respects_the_one_answer_at_a_time_rule(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(answers=["slow", "never"], delay_seconds=0.5))
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]
    conversation = await create_conversation(client, headers)

    first = asyncio.create_task(
        call_tool(client, token, "ask", {"question": "A", "conversation_id": conversation["id"]})
    )
    await asyncio.sleep(0.2)
    second = await call_tool(
        client, token, "ask", {"question": "B", "conversation_id": conversation["id"]}
    )

    assert "(code: run_in_progress)" in second.tool_error
    assert (await first).structured["answer"] == "slow"


async def test_a_failed_run_is_reported_as_a_tool_error(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["unused"], delay_seconds=1.0))
    app.state.agent_limits = replace(app.state.agent_limits, run_timeout_seconds=0.05)
    token = await _account(client)

    reply = await call_tool(client, token, "ask", {"question": "Anything new?"})

    assert reply.result["isError"] is True
    assert "(code: run_timeout)" in reply.tool_error


async def test_a_client_that_goes_away_stops_its_run(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["x" * 4000], chunk_delay_seconds=0.02))
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]
    conversation = await create_conversation(client, headers)

    call = asyncio.create_task(
        call_tool(
            client, token, "ask", {"question": "Long?", "conversation_id": conversation["id"]}
        )
    )
    async with asyncio.timeout(5):
        while not await runs_for(app, conversation["id"]):
            await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    async with asyncio.timeout(5):
        while (await runs_for(app, conversation["id"]))[0].status.value != "cancelled":
            await asyncio.sleep(0.05)
    [run] = await runs_for(app, conversation["id"])
    assert run.error_code == "client_disconnected"


# --- documents and resources -----------------------------------------------------------


async def test_search_documents_returns_passages_as_untrusted_text(
    app: FastAPI, client: AsyncClient
) -> None:
    await app.state.embedding_provider.aclose()
    app.state.embedding_provider = FakeEmbeddings()
    app.state.agent_limits = replace(app.state.agent_limits, document_min_similarity=0.05)
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]
    conversation_id = str((await create_conversation(client, headers))["id"])

    empty = await call_tool(
        client, token, "search_documents", {"conversation_id": conversation_id, "query": "revenue"}
    )
    uploaded = await client.post(
        f"/api/v1/conversations/{conversation_id}/documents",
        files={
            "file": (
                "report.pdf",
                make_pdf(["Quarterly revenue grew twelve percent."]),
                "application/pdf",
            )
        },
        headers=headers,
    )
    document_id = uploaded.json()["id"]
    async with asyncio.timeout(15):
        while (
            await client.get(
                f"/api/v1/conversations/{conversation_id}/documents/{document_id}",
                headers=headers,
            )
        ).json()["status"] == "processing":
            await asyncio.sleep(0.05)
    found = await call_tool(
        client,
        token,
        "search_documents",
        {"conversation_id": conversation_id, "query": "quarterly revenue"},
    )

    assert empty.structured == {"conversation_id": conversation_id, "documents": [], "passages": []}
    assert found.structured["documents"] == ["report.pdf"]
    [passage] = found.structured["passages"]
    assert passage["document_id"] == document_id
    assert passage["filename"] == "report.pdf"
    assert passage["page"] == 1
    assert "twelve percent" in passage["untrusted_text"]


async def test_a_conversation_reads_as_a_markdown_resource(
    app: FastAPI, client: AsyncClient
) -> None:
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]
    bob = await _account(client, "bob@example.com")
    conversation = await create_conversation(client, headers, title="Trip ideas")
    await seed_messages(
        app,
        user_email="user@example.com",
        conversation_id=uuid.UUID(conversation["id"]),
        contents=[("user", "Where to go?"), ("assistant", "Lisbon.")],
    )
    uri = f"conversation://{conversation['id']}"

    templates = (await mcp_request(client, token, "resources/templates/list", {})).result
    mine = await mcp_request(client, token, "resources/read", {"uri": uri})
    theirs = await mcp_request(client, bob, "resources/read", {"uri": uri})

    assert [t["uriTemplate"] for t in templates["resourceTemplates"]] == [
        "conversation://{conversation_id}"
    ]
    [contents] = mine.result["contents"]
    assert contents["mimeType"] == "text/markdown"
    assert contents["text"].startswith("# Trip ideas\n")
    assert "Where to go?" in contents["text"] and "Lisbon." in contents["text"]
    assert "error" in theirs.final
    assert "Lisbon" not in str(theirs.final)


async def test_a_body_that_is_not_json_rpc_is_rejected(client: AsyncClient) -> None:
    token = await _account(client)

    reply = await mcp_post(client, token, {"not": "json-rpc"})

    assert reply.status == 400
