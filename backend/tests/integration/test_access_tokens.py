"""Personal access tokens: minting, listing, revoking, and what they can reach (ADR-0019)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text, update

from app.core.security import hash_opaque_token
from app.db.session import Database
from app.models.personal_access_token import PersonalAccessToken
from tests.fakes import RecordingEmailSender
from tests.integration.conftest import auth_header, register
from tests.integration.mcp_support import create_token, mcp_request

pytestmark = pytest.mark.integration


async def _stored(app: FastAPI) -> list[PersonalAccessToken]:
    database: Database = app.state.database
    async with database.open_session() as session:
        return list((await session.execute(select(PersonalAccessToken))).scalars())


async def test_a_token_is_shown_once_and_stored_only_as_a_hash(
    app: FastAPI, client: AsyncClient
) -> None:
    headers = auth_header(await register(client))

    created = await create_token(client, headers, name="  Claude   Desktop ", expires_in_days=7)

    token = created["token"]
    assert re.fullmatch(r"wsc_pat_[A-Za-z0-9_-]{43}", token)
    assert created["name"] == "Claude Desktop"
    assert created["token_prefix"] == token[:12]
    assert created["scopes"] == ["conversations:read", "runs:create"]
    expires_at = datetime.fromisoformat(created["expires_at"])
    assert timedelta(days=6, hours=23) < expires_at - datetime.now(UTC) <= timedelta(days=7)

    [row] = await _stored(app)
    assert row.token_hash == hash_opaque_token(token)
    assert token not in {row.token_hash, row.token_prefix, row.name}

    listed = await client.get("/api/v1/access-tokens", headers=headers)
    assert listed.status_code == 200
    [item] = listed.json()["items"]
    assert "token" not in item
    assert item["id"] == created["id"]
    assert item["last_used_at"] is None


async def test_scopes_are_validated_and_deduplicated(client: AsyncClient) -> None:
    headers = auth_header(await register(client))

    unknown = await client.post(
        "/api/v1/access-tokens",
        json={"name": "x", "scopes": ["conversations:write"]},
        headers=headers,
    )
    empty = await client.post(
        "/api/v1/access-tokens", json={"name": "x", "scopes": []}, headers=headers
    )
    repeated = await create_token(client, headers, scopes=("runs:create", "runs:create"))

    assert unknown.status_code == empty.status_code == 422
    assert repeated["scopes"] == ["runs:create"]


async def test_names_and_lifetimes_are_bounded(client: AsyncClient) -> None:
    headers = auth_header(await register(client))

    blank = await client.post(
        "/api/v1/access-tokens",
        json={"name": "   ", "scopes": ["runs:create"]},
        headers=headers,
    )
    too_long = await client.post(
        "/api/v1/access-tokens",
        json={"name": "x", "scopes": ["runs:create"], "expires_in_days": 91},
        headers=headers,
    )

    assert blank.status_code == 422
    assert too_long.status_code == 422
    assert too_long.json()["error"]["details"]["max_days"] == 90


async def test_an_account_has_a_capped_number_of_live_tokens(
    app: FastAPI, client: AsyncClient
) -> None:
    app.state.settings.max_access_tokens_per_user = 2
    headers = auth_header(await register(client))
    first = await create_token(client, headers)
    await create_token(client, headers)

    refused = await client.post(
        "/api/v1/access-tokens", json={"name": "x", "scopes": ["runs:create"]}, headers=headers
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "access_token_limit_reached"

    # Revoking one frees its place.
    await client.delete(f"/api/v1/access-tokens/{first['id']}", headers=headers)
    await create_token(client, headers)


async def test_a_user_sees_and_revokes_only_their_own_tokens(client: AsyncClient) -> None:
    alice = auth_header(await register(client, email="alice@example.com"))
    bob = auth_header(await register(client, email="bob@example.com"))
    alices = await create_token(client, alice)

    bobs_view = await client.get("/api/v1/access-tokens", headers=bob)
    bobs_revoke = await client.delete(f"/api/v1/access-tokens/{alices['id']}", headers=bob)

    assert bobs_view.json()["items"] == []
    assert bobs_revoke.status_code == 404
    assert bobs_revoke.json()["error"]["code"] == "access_token_not_found"
    # Still Alice's, still working.
    assert (await mcp_request(client, alices["token"], "tools/list", {})).status == 200


async def test_a_revoked_token_stops_working_and_leaves_the_list(client: AsyncClient) -> None:
    headers = auth_header(await register(client))
    created = await create_token(client, headers)

    revoked = await client.delete(f"/api/v1/access-tokens/{created['id']}", headers=headers)
    again = await client.delete(f"/api/v1/access-tokens/{created['id']}", headers=headers)

    assert revoked.status_code == 204
    assert again.status_code == 404
    assert (await client.get("/api/v1/access-tokens", headers=headers)).json()["items"] == []
    assert (await mcp_request(client, created["token"], "tools/list", {})).status == 401


async def test_an_expired_token_is_listed_but_refused(app: FastAPI, client: AsyncClient) -> None:
    headers = auth_header(await register(client))
    created = await create_token(client, headers)
    database: Database = app.state.database
    async with database.open_session() as session:
        await session.execute(
            update(PersonalAccessToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    assert (await mcp_request(client, created["token"], "tools/list", {})).status == 401
    [item] = (await client.get("/api/v1/access-tokens", headers=headers)).json()["items"]
    assert item["id"] == created["id"]


async def test_a_token_cannot_call_the_rest_api(client: AsyncClient) -> None:
    """It works at the MCP endpoint only, so it can never mint or revoke tokens."""
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]
    as_token = {"Authorization": f"Bearer {token}"}

    for method, path in [
        ("GET", "/api/v1/users/me"),
        ("GET", "/api/v1/conversations"),
        ("GET", "/api/v1/access-tokens"),
        ("POST", "/api/v1/access-tokens"),
    ]:
        response = await client.request(
            method, path, headers=as_token, json={"name": "x", "scopes": ["runs:create"]}
        )
        assert response.status_code == 401, (method, path)


async def test_a_password_reset_revokes_every_token(
    client: AsyncClient, email_outbox: RecordingEmailSender
) -> None:
    headers = auth_header(await register(client, email="reset@example.com"))
    token = (await create_token(client, headers))["token"]
    await client.post("/api/v1/auth/forgot-password", json={"email": "reset@example.com"})
    match = re.search(r"/reset-password\?token=(\S+)", email_outbox.last.body)
    assert match

    reset = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": match.group(1), "password": "an-entirely-different-passphrase"},
    )

    assert reset.status_code == 200
    assert (await mcp_request(client, token, "tools/list", {})).status == 401


async def test_a_deactivated_account_loses_its_tokens_at_once(
    app: FastAPI, client: AsyncClient
) -> None:
    headers = auth_header(await register(client, email="leaver@example.com"))
    token = (await create_token(client, headers))["token"]
    database: Database = app.state.database
    async with database.open_session() as session:
        await session.execute(text("UPDATE users SET is_active = false"))
        await session.commit()

    assert (await mcp_request(client, token, "tools/list", {})).status == 401


async def test_use_is_recorded_at_most_once_a_minute(app: FastAPI, client: AsyncClient) -> None:
    headers = auth_header(await register(client))
    token = (await create_token(client, headers))["token"]

    await mcp_request(client, token, "tools/list", {})
    [first] = await _stored(app)
    await mcp_request(client, token, "tools/list", {})
    [second] = await _stored(app)

    assert first.last_used_at is not None
    assert second.last_used_at == first.last_used_at
