"""Refresh rotation and reuse detection.

The security property under test: a refresh token is single-use. If one is
presented twice, the second presentation is treated as evidence of theft and
the entire token family is revoked.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.models.auth_session import AuthSession
from tests.integration.conftest import put_refresh_cookie, read_refresh_cookie, register

pytestmark = pytest.mark.integration

REFRESH_URL = "/api/v1/auth/refresh"


async def test_refresh_rotates_the_token(client: AsyncClient) -> None:
    await register(client, email="rotate@example.com")
    first = read_refresh_cookie(client)

    response = await client.post(REFRESH_URL)

    assert response.status_code == 200
    second = read_refresh_cookie(client)
    assert second != first, "the refresh token must change on every use"
    assert response.json()["access_token"]


async def test_reusing_a_rotated_token_revokes_the_whole_family(
    client: AsyncClient, settings: Settings
) -> None:
    await register(client, email="stolen@example.com")
    stolen = read_refresh_cookie(client)

    # The legitimate client refreshes; `stolen` is now a retired token.
    assert (await client.post(REFRESH_URL)).status_code == 200
    live = read_refresh_cookie(client)

    # An attacker replays the retired token.
    put_refresh_cookie(client, stolen)
    replay = await client.post(REFRESH_URL)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "invalid_token"

    # ...and the legitimate token is now dead too. Losing the session is the
    # correct outcome: one of the two holders is an attacker and the server
    # cannot tell which.
    put_refresh_cookie(client, live)
    assert (await client.post(REFRESH_URL)).status_code == 401

    engine = create_async_engine(str(settings.database_url))
    async with engine.connect() as connection:
        unrevoked = await connection.execute(
            select(func.count()).select_from(AuthSession).where(AuthSession.revoked_at.is_(None))
        )
        assert unrevoked.scalar_one() == 0
    await engine.dispose()


async def test_rotation_keeps_one_family_and_links_the_chain(
    client: AsyncClient, settings: Settings
) -> None:
    await register(client, email="chain@example.com")
    for _ in range(3):
        assert (await client.post(REFRESH_URL)).status_code == 200

    engine = create_async_engine(str(settings.database_url))
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(AuthSession.family_id, AuthSession.replaced_by_id, AuthSession.revoked_at)
            )
        ).all()
    await engine.dispose()

    assert len(rows) == 4, "one row per issued token"
    assert len({row.family_id for row in rows}) == 1, "rotation stays inside one family"
    # Three retired tokens each point at their successor; the live one does not.
    assert sum(1 for row in rows if row.replaced_by_id is not None) == 3
    assert sum(1 for row in rows if row.revoked_at is None) == 1


async def test_refresh_without_a_cookie_is_rejected(client: AsyncClient) -> None:
    response = await client.post(REFRESH_URL)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_refresh_rejects_a_forged_token(client: AsyncClient) -> None:
    await register(client, email="forged@example.com")
    put_refresh_cookie(client, "not-a-real-token")

    response = await client.post(REFRESH_URL)

    assert response.status_code == 401
