"""Users' own LLM API keys and the model picker, end to end (ADR-0020).

Every provider here is a fake: ``app.state.llm_factory`` is swapped for a
recorder, so a "key check" or a run with a user's key never leaves the test.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, text, update

from app.core.crypto import SecretBox
from app.core.logging import JsonFormatter
from app.db.session import Database
from app.models.llm_credential import LLMCredential
from app.providers.llm import (
    LLMKeyRejected,
    LLMModelUnavailable,
    LLMPermanentError,
    LLMTransientError,
)
from tests.fakes import FakeLLM, RecordingLLMFactory
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.mcp_support import call_tool, create_token
from tests.integration.runs_support import (
    install_providers,
    messages_for,
    parse_sse,
    post_run,
    runs_for,
)

pytestmark = pytest.mark.integration

SETTINGS_URL = "/api/v1/users/me/llm-settings"
SELECTION_URL = "/api/v1/users/me/llm-selection"
# Distinctive, so a leak anywhere is easy to find.
OPENAI_KEY = "sk-user-owned-0123456789-leakcheck-wxyz"
ANTHROPIC_KEY = "sk-ant-second-key-9876543210-leakcheck-abcd"


def credential_url(provider: str) -> str:
    return f"/api/v1/users/me/llm-credentials/{provider}"


@pytest.fixture
def factory(app: FastAPI, client: AsyncClient) -> RecordingLLMFactory:
    """Installed after the lifespan has built the real one."""
    recorder = RecordingLLMFactory()
    app.state.llm_factory = recorder
    return recorder


async def save_key(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    provider: str = "openai",
    model: str = "gpt-4.1-mini",
    api_key: str = OPENAI_KEY,
) -> Response:
    return await client.put(
        credential_url(provider), json={"model": model, "api_key": api_key}, headers=headers
    )


async def choose(
    client: AsyncClient, headers: dict[str, str], provider: str | None, model: str | None
) -> Response:
    return await client.put(
        SELECTION_URL, json={"provider": provider, "model": model}, headers=headers
    )


async def stored_credentials(app: FastAPI) -> list[LLMCredential]:
    database: Database = app.state.database
    async with database.open_session() as session:
        return list((await session.execute(select(LLMCredential))).scalars())


def selection(response: Response) -> Any:
    return response.json()["selection"]


# --- settings ----------------------------------------------------------------


async def test_settings_list_the_providers_and_the_default_model(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app, llm=FakeLLM(model="server-model"))
    headers = auth_header(await register(client))

    response = await client.get(SETTINGS_URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["credentials"] == []
    assert body["server_model"] == {"model": "server-model"}
    assert body["selection"] == {"source": "server", "provider": None, "model": "server-model"}
    ids = [provider["id"] for provider in body["providers"]]
    assert {"openai", "anthropic", "google", "openrouter"} <= set(ids)
    # Presets publish no base URL: where a key is sent is the server's business.
    assert all("base_url" not in provider for provider in body["providers"])


async def test_with_no_key_and_no_default_nothing_is_selected(
    app: FastAPI, client: AsyncClient
) -> None:
    app.state.llm_provider = None
    headers = auth_header(await register(client))

    body = (await client.get(SETTINGS_URL, headers=headers)).json()

    assert body["server_model"] is None
    assert body["selection"] is None


# --- saving keys -------------------------------------------------------------


async def test_a_saved_key_is_checked_encrypted_selected_and_never_returned(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    headers = auth_header(await register(client))

    saved = await save_key(client, headers)
    listed = await client.get(SETTINGS_URL, headers=headers)

    assert saved.status_code == 200, saved.text
    [credential] = saved.json()["credentials"]
    assert credential["provider"] == "openai"
    assert credential["key_hint"] == "wxyz"
    assert credential["is_active"] is True
    assert selection(saved) == {"source": "user", "provider": "openai", "model": "gpt-4.1-mini"}
    # One check, with the key and model the user chose, before saving.
    assert factory.calls == [("openai", "gpt-4.1-mini", OPENAI_KEY)]
    assert factory.built[0].calls, "the key was never actually tried"
    for response in (saved, listed):
        assert OPENAI_KEY not in response.text
    [row] = await stored_credentials(app)
    assert OPENAI_KEY.encode() not in row.encrypted_api_key


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (LLMKeyRejected("401"), 422, "llm_key_invalid"),
        (LLMPermanentError("404 model"), 422, "llm_model_rejected"),
        (LLMTransientError("503"), 503, "llm_key_check_unavailable"),
    ],
)
async def test_a_key_that_fails_its_check_is_not_saved(
    app: FastAPI, client: AsyncClient, error: Exception, status: int, code: str
) -> None:
    app.state.llm_factory = RecordingLLMFactory(lambda *_: FakeLLM(answer_error=error))
    headers = auth_header(await register(client))

    response = await save_key(client, headers)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert OPENAI_KEY not in response.text
    assert await stored_credentials(app) == []


@pytest.mark.parametrize(
    ("provider", "status"),
    [("magic", 422), ("http:%2F%2F169.254.169.254", 404), ("OpenAI", 422)],
    ids=["unknown preset", "url", "wrong case"],
)
async def test_an_unknown_provider_is_refused_without_a_call(
    client: AsyncClient, factory: RecordingLLMFactory, provider: str, status: int
) -> None:
    headers = auth_header(await register(client))

    response = await save_key(client, headers, provider=provider)

    assert response.status_code == status
    assert factory.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"model": "gpt 4", "api_key": OPENAI_KEY},
        {"model": "../../admin", "api_key": OPENAI_KEY},
        {"model": "gpt-4.1-mini", "api_key": "has space in it"},
        {"model": "gpt-4.1-mini", "api_key": "short"},
        {"model": "gpt-4.1-mini"},
    ],
    ids=["space in model", "path in model", "space in key", "short key", "no key"],
)
async def test_malformed_input_is_refused_without_a_call(
    client: AsyncClient, factory: RecordingLLMFactory, body: dict[str, str]
) -> None:
    headers = auth_header(await register(client))

    response = await client.put(credential_url("openai"), json=body, headers=headers)

    assert response.status_code == 422
    assert factory.calls == []
    assert OPENAI_KEY not in response.text


async def test_a_pasted_key_is_trimmed(client: AsyncClient, factory: RecordingLLMFactory) -> None:
    headers = auth_header(await register(client))

    response = await save_key(client, headers, api_key=f"  {OPENAI_KEY}\n")

    assert response.status_code == 200
    assert factory.keys_used == [OPENAI_KEY]


async def test_a_second_provider_is_kept_alongside_and_becomes_active(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    headers = auth_header(await register(client))
    await save_key(client, headers)

    response = await save_key(
        client, headers, provider="anthropic", model="claude-sonnet-5-5", api_key=ANTHROPIC_KEY
    )

    credentials = {item["provider"]: item for item in response.json()["credentials"]}
    assert set(credentials) == {"openai", "anthropic"}
    assert credentials["anthropic"]["is_active"] is True
    assert credentials["openai"]["is_active"] is False
    assert selection(response)["provider"] == "anthropic"


async def test_saving_again_replaces_that_providers_key(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    headers = auth_header(await register(client))
    await save_key(client, headers)

    response = await save_key(client, headers, api_key=ANTHROPIC_KEY)

    [credential] = response.json()["credentials"]
    assert credential["key_hint"] == "abcd"
    assert len(await stored_credentials(app)) == 1


async def test_key_checks_are_rate_limited_per_user(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    """Otherwise the endpoint is a free oracle for testing stolen keys."""
    app.state.settings.rate_limit_llm_key_checks = 2
    headers = auth_header(await register(client))

    statuses = [(await save_key(client, headers)).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]
    assert len(factory.calls) == 2


# --- switching models --------------------------------------------------------


async def test_switching_between_saved_keys_is_instant(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    """The picker's main job. Keys were checked when saved; a switch makes no
    provider call."""
    await install_providers(app)
    headers = auth_header(await register(client))
    await save_key(client, headers)
    await save_key(
        client, headers, provider="anthropic", model="claude-sonnet-5-5", api_key=ANTHROPIC_KEY
    )
    calls_before = len(factory.calls)

    switched = await choose(client, headers, "openai", "gpt-4o")

    assert switched.status_code == 200, switched.text
    assert selection(switched) == {"source": "user", "provider": "openai", "model": "gpt-4o"}
    assert len(factory.calls) == calls_before
    # The other provider's key and its model are remembered for later.
    credentials = {item["provider"]: item for item in switched.json()["credentials"]}
    assert credentials["anthropic"] == {
        **credentials["anthropic"],
        "model": "claude-sonnet-5-5",
        "is_active": False,
    }


async def test_switching_back_to_the_default_model(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    await install_providers(app, llm=FakeLLM(model="server-model"))
    headers = auth_header(await register(client))
    await save_key(client, headers)

    response = await choose(client, headers, None, None)

    assert selection(response) == {"source": "server", "provider": None, "model": "server-model"}
    assert all(not item["is_active"] for item in response.json()["credentials"])


async def test_there_is_no_default_to_switch_to_without_a_server_key(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    app.state.llm_provider = None
    headers = auth_header(await register(client))
    await save_key(client, headers)

    response = await choose(client, headers, None, None)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "llm_default_unavailable"


async def test_a_provider_without_a_key_cannot_be_selected(
    client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    headers = auth_header(await register(client))

    response = await choose(client, headers, "anthropic", "claude-sonnet-5-5")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "llm_credential_not_found"


@pytest.mark.parametrize(
    "body",
    [{"provider": "openai"}, {"model": "gpt-4o"}, {"provider": "openai", "model": "gpt 4"}],
    ids=["no model", "no provider", "bad model"],
)
async def test_a_half_selection_is_refused(
    client: AsyncClient, factory: RecordingLLMFactory, body: dict[str, str]
) -> None:
    headers = auth_header(await register(client))
    await save_key(client, headers)

    response = await client.put(SELECTION_URL, json=body, headers=headers)

    assert response.status_code == 422


# --- removing keys -----------------------------------------------------------


async def test_removing_a_key_is_idempotent(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    headers = auth_header(await register(client))
    await save_key(client, headers)

    first = await client.delete(credential_url("openai"), headers=headers)
    second = await client.delete(credential_url("openai"), headers=headers)

    assert (first.status_code, second.status_code) == (204, 204)
    assert await stored_credentials(app) == []


async def test_removing_the_active_key_falls_back_to_the_default(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    await install_providers(app, llm=FakeLLM(model="server-model"))
    headers = auth_header(await register(client))
    await save_key(client, headers)

    await client.delete(credential_url("openai"), headers=headers)
    body = (await client.get(SETTINGS_URL, headers=headers)).json()

    assert body["selection"]["source"] == "server"


async def test_without_a_default_another_saved_key_answers(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    """Removing the active key must not strand a user who has another."""
    app.state.llm_provider = None
    headers = auth_header(await register(client))
    await save_key(client, headers)
    await save_key(
        client, headers, provider="anthropic", model="claude-sonnet-5-5", api_key=ANTHROPIC_KEY
    )

    await client.delete(credential_url("anthropic"), headers=headers)
    body = (await client.get(SETTINGS_URL, headers=headers)).json()

    assert body["selection"] == {"source": "user", "provider": "openai", "model": "gpt-4.1-mini"}


# --- access ------------------------------------------------------------------


async def test_the_settings_need_a_signed_in_user(client: AsyncClient) -> None:
    assert (await client.get(SETTINGS_URL)).status_code == 401
    assert (await client.put(credential_url("openai"), json={})).status_code == 401
    assert (await client.delete(credential_url("openai"))).status_code == 401
    assert (await client.put(SELECTION_URL, json={})).status_code == 401


async def test_an_mcp_token_cannot_read_replace_or_switch_keys(
    client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    """ADR-0019: a personal access token works at /mcp and nowhere else."""
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]
    pat = {"Authorization": f"Bearer {token}"}

    assert (await client.get(SETTINGS_URL, headers=pat)).status_code == 401
    assert (await save_key(client, pat)).status_code == 401
    assert (await choose(client, pat, None, None)).status_code == 401
    assert factory.calls == []


# --- runs --------------------------------------------------------------------


async def test_a_run_uses_the_selected_key_and_model(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    server, _ = await install_providers(app, llm=FakeLLM(model="server-model"))
    headers = auth_header(await register(client))
    await save_key(client, headers)
    await save_key(
        client, headers, provider="anthropic", model="claude-sonnet-5-5", api_key=ANTHROPIC_KEY
    )
    await choose(client, headers, "openai", "gpt-4o")
    conversation = await create_conversation(client, headers)

    response = await post_run(client, headers, conversation["id"], content="hello")

    assert parse_sse(response.text).terminal["type"] == "message.completed"
    # Built from the selected key with the selected model; nothing else ran.
    assert factory.calls[-1] == ("openai", "gpt-4o", OPENAI_KEY)
    assert factory.built[-1].calls
    assert server.calls == []
    [run] = await runs_for(app, conversation["id"])
    assert (run.provider, run.model) == ("openai", "gpt-4o")


async def test_the_default_model_answers_once_selected_even_with_keys_saved(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    server, _ = await install_providers(app, llm=FakeLLM(model="server-model"))
    headers = auth_header(await register(client))
    await save_key(client, headers)
    await choose(client, headers, None, None)
    calls_before = len(factory.calls)
    conversation = await create_conversation(client, headers)

    response = await post_run(client, headers, conversation["id"], content="hello")

    assert parse_sse(response.text).terminal["type"] == "message.completed"
    assert server.calls
    assert len(factory.calls) == calls_before
    [run] = await runs_for(app, conversation["id"])
    assert (run.provider, run.model) == ("openai_compatible", "server-model")


async def test_with_no_key_and_no_default_a_run_asks_for_a_key(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app)
    app.state.llm_provider = None
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)

    response = await post_run(client, headers, conversation["id"], content="hello")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "llm_key_required"
    # Refused before anything was stored: no orphaned question.
    assert await messages_for(app, conversation["id"]) == []
    assert await runs_for(app, conversation["id"]) == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (LLMKeyRejected("401"), "llm_key_rejected"),
        (LLMModelUnavailable("404"), "llm_model_unavailable"),
    ],
)
async def test_failures_the_user_can_fix_say_how(
    app: FastAPI, client: AsyncClient, error: Exception, code: str
) -> None:
    """A key revoked since it was saved, or a model name the provider lacks."""
    await install_providers(app)
    headers = auth_header(await register(client))
    app.state.llm_factory = RecordingLLMFactory()
    await save_key(client, headers)
    app.state.llm_factory = RecordingLLMFactory(lambda *_: FakeLLM(answer_error=error))
    conversation = await create_conversation(client, headers)

    response = await post_run(client, headers, conversation["id"], content="hello")

    failure = parse_sse(response.text).terminal["error"]
    assert failure["code"] == code
    assert failure["retryable"] is False


async def test_a_key_saved_under_another_encryption_key_is_reported_not_replaced(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    """CREDENTIAL_ENCRYPTION_KEY changed: the user is told, rather than quietly
    switched to the server's model."""
    server, _ = await install_providers(app)
    headers = auth_header(await register(client))
    await save_key(client, headers)
    app.state.secret_box = SecretBox("a-completely-different-encryption-key-0123456789")
    conversation = await create_conversation(client, headers)

    response = await post_run(client, headers, conversation["id"], content="hello")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "llm_key_unreadable"
    assert server.calls == []


# --- isolation ---------------------------------------------------------------


async def test_one_users_keys_are_invisible_and_unusable_to_another(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    server, _ = await install_providers(app)
    alice = auth_header(await register(client, email="alice@example.com"))
    bob = auth_header(await register(client, email="bob@example.com"))
    await save_key(client, alice)
    bob_conversation = await create_conversation(client, bob)

    bob_settings = await client.get(SETTINGS_URL, headers=bob)
    bob_switch = await choose(client, bob, "openai", "gpt-4o")
    bob_delete = await client.delete(credential_url("openai"), headers=bob)
    bob_run = await post_run(client, bob, bob_conversation["id"], content="hello")

    assert bob_settings.json()["credentials"] == []
    assert bob_switch.status_code == 404
    assert bob_delete.status_code == 204
    assert parse_sse(bob_run.text).terminal["type"] == "message.completed"
    # Bob's run used the server's model; Alice's key was never decrypted for it.
    assert server.calls
    assert factory.keys_used == [OPENAI_KEY]  # only Alice's own save-time check
    assert len(await stored_credentials(app)) == 1  # Bob's delete touched nothing


async def test_a_ciphertext_copied_to_another_user_does_not_decrypt(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    """Each key is bound to its owner's id, so a row moved between users (by
    a SQL injection elsewhere, say) yields nothing usable."""
    await install_providers(app)
    alice = auth_header(await register(client, email="alice@example.com"))
    bob = auth_header(await register(client, email="bob@example.com"))
    await save_key(client, alice)
    await save_key(client, bob, api_key=ANTHROPIC_KEY)
    alice_id = (await client.get("/api/v1/users/me", headers=alice)).json()["id"]
    bob_id = (await client.get("/api/v1/users/me", headers=bob)).json()["id"]
    database: Database = app.state.database
    async with database.open_session() as session:
        [stolen] = (
            await session.execute(
                select(LLMCredential.encrypted_api_key).where(
                    LLMCredential.user_id == uuid.UUID(alice_id)
                )
            )
        ).one()
        await session.execute(
            update(LLMCredential)
            .where(LLMCredential.user_id == uuid.UUID(bob_id))
            .values(encrypted_api_key=stolen)
        )
        await session.commit()
    conversation = await create_conversation(client, bob)

    response = await post_run(client, bob, conversation["id"], content="hello")

    assert response.json()["error"]["code"] == "llm_key_unreadable"
    assert OPENAI_KEY not in factory.keys_used[2:]


async def test_deleting_the_account_deletes_its_keys(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    headers = auth_header(await register(client))
    await save_key(client, headers)
    database: Database = app.state.database
    async with database.open_session() as session:
        await session.execute(text("DELETE FROM users"))
        await session.commit()

    assert await stored_credentials(app) == []


# --- MCP ---------------------------------------------------------------------


async def test_mcp_ask_uses_the_selected_key(
    app: FastAPI, client: AsyncClient, factory: RecordingLLMFactory
) -> None:
    server, _ = await install_providers(app)
    headers = auth_header(await register(client))
    await save_key(client, headers)
    token = (await create_token(client, headers))["token"]

    asked = await call_tool(client, token, "ask", {"question": "hello"})

    assert asked.structured["answer"]
    assert len(factory.calls) == 2  # the save-time check, then the run
    assert server.calls == []


async def test_mcp_ask_without_any_model_asks_for_a_key_and_leaves_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(app)
    app.state.llm_provider = None
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]

    refused = await call_tool(client, token, "ask", {"question": "hello"})
    listed = await client.get("/api/v1/conversations", headers=headers)

    assert "(code: llm_key_required)" in refused.tool_error
    assert listed.json()["items"] == []


# --- logs --------------------------------------------------------------------


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
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


async def test_keys_never_reach_the_logs(
    app: FastAPI,
    client: AsyncClient,
    factory: RecordingLLMFactory,
    captured_logs: io.StringIO,
) -> None:
    await install_providers(app)
    headers = auth_header(await register(client))
    await save_key(client, headers)
    await save_key(
        client, headers, provider="anthropic", model="claude-sonnet-5-5", api_key=ANTHROPIC_KEY
    )
    await choose(client, headers, "openai", "gpt-4o")
    conversation = await create_conversation(client, headers)
    await post_run(client, headers, conversation["id"], content="hello")
    app.state.llm_factory = RecordingLLMFactory(
        lambda *_: FakeLLM(answer_error=LLMKeyRejected(f"bad key {OPENAI_KEY}"))
    )
    await save_key(client, headers)  # a failed check
    await client.delete(credential_url("openai"), headers=headers)

    output = captured_logs.getvalue()
    for key in (OPENAI_KEY, ANTHROPIC_KEY):
        assert key not in output
        assert key[-12:] not in output
    events = {json.loads(line)["event"] for line in output.splitlines()}
    assert {"llm_credential_saved", "llm_credential_deleted"} <= events
