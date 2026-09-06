"""Cross-user isolation.

The rule under test is blunt: a signed-in user must not be able to observe that
another user's conversation exists. Every route answers 404 — the same answer a
made-up UUID gets — so a client that guesses ids learns nothing from the
difference between "yours and missing" and "real but not yours".
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.integration.conftest import auth_header, create_conversation, register, seed_messages

OWNER_EMAIL = "owner@example.com"
INTRUDER_EMAIL = "intruder@example.com"


@pytest.fixture
async def owned_conversation(client: AsyncClient, app: FastAPI) -> dict[str, str]:
    """A conversation with messages, owned by someone other than the caller."""
    owner = await register(client, email=OWNER_EMAIL, display_name="Owner")
    conversation = await create_conversation(client, auth_header(owner), title="Private")
    await seed_messages(
        app,
        user_email=OWNER_EMAIL,
        conversation_id=uuid.UUID(conversation["id"]),
        contents=[("user", "a private question"), ("assistant", "a private answer")],
    )
    intruder = await register(client, email=INTRUDER_EMAIL, display_name="Intruder")
    return {"id": conversation["id"], "intruder_token": intruder["access_token"]}


def _intruder_headers(owned: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {owned['intruder_token']}"}


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"title": "Renamed by an intruder"}),
        ("DELETE", "", None),
        ("GET", "/messages", None),
    ],
)
async def test_another_users_conversation_is_not_found(
    client: AsyncClient,
    owned_conversation: dict[str, str],
    method: str,
    suffix: str,
    body: dict[str, str] | None,
) -> None:
    response = await client.request(
        method,
        f"/api/v1/conversations/{owned_conversation['id']}{suffix}",
        json=body,
        headers=_intruder_headers(owned_conversation),
    )

    assert response.status_code == 404, response.text
    # 403 would confirm the id names something real.
    assert response.json()["error"]["code"] == "conversation_not_found"


async def test_a_guessed_uuid_is_indistinguishable_from_someone_elses(
    client: AsyncClient, owned_conversation: dict[str, str]
) -> None:
    headers = _intruder_headers(owned_conversation)

    real_but_not_mine = await client.get(
        f"/api/v1/conversations/{owned_conversation['id']}", headers=headers
    )
    pure_invention = await client.get(f"/api/v1/conversations/{uuid.uuid4()}", headers=headers)

    assert real_but_not_mine.status_code == pure_invention.status_code == 404
    assert real_but_not_mine.json()["error"]["code"] == pure_invention.json()["error"]["code"]
    # Identical bodies apart from the per-request id: no oracle anywhere.
    assert (
        real_but_not_mine.json()["error"]["message"] == (pure_invention.json()["error"]["message"])
    )


async def test_another_users_conversation_never_appears_in_a_listing(
    client: AsyncClient, owned_conversation: dict[str, str]
) -> None:
    response = await client.get(
        "/api/v1/conversations", headers=_intruder_headers(owned_conversation)
    )

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_an_intruders_rename_does_not_touch_the_row(
    client: AsyncClient, owned_conversation: dict[str, str]
) -> None:
    await client.patch(
        f"/api/v1/conversations/{owned_conversation['id']}",
        json={"title": "Renamed by an intruder"},
        headers=_intruder_headers(owned_conversation),
    )

    owner = await client.post(
        "/api/v1/auth/login",
        json={"email": OWNER_EMAIL, "password": "correct-horse-battery-staple"},
    )
    still_mine = await client.get(
        f"/api/v1/conversations/{owned_conversation['id']}",
        headers=auth_header(owner.json()),
    )
    assert still_mine.json()["title"] == "Private"


async def test_every_conversation_route_requires_authentication(
    client: AsyncClient, owned_conversation: dict[str, str]
) -> None:
    conversation_id = owned_conversation["id"]
    for method, path in [
        ("GET", "/api/v1/conversations"),
        ("POST", "/api/v1/conversations"),
        ("GET", f"/api/v1/conversations/{conversation_id}"),
        ("PATCH", f"/api/v1/conversations/{conversation_id}"),
        ("DELETE", f"/api/v1/conversations/{conversation_id}"),
        ("GET", f"/api/v1/conversations/{conversation_id}/messages"),
    ]:
        response = await client.request(method, path, json={"title": "x"})
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"
        assert response.json()["error"]["code"] == "authentication_required"
