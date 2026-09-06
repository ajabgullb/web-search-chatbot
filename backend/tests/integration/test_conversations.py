"""Conversation CRUD, cursor pagination, and soft deletion."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.integration.conftest import auth_header, create_conversation, register, seed_messages

USER_EMAIL = "chatter@example.com"


@pytest.fixture
async def headers(client: AsyncClient) -> dict[str, str]:
    return auth_header(await register(client, email=USER_EMAIL, display_name="Chatter"))


async def _walk(
    client: AsyncClient, headers: dict[str, str], *, limit: int, path: str = "/api/v1/conversations"
) -> list[dict[str, Any]]:
    """Follow every cursor to the end and return the rows collected."""
    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):  # a runaway cursor loop should fail the test, not hang it
        query = f"?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        response = await client.get(f"{path}{query}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        collected.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return collected
    raise AssertionError("pagination did not terminate")


class TestCrud:
    async def test_create_defaults_to_a_placeholder_title(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers)
        assert conversation["title"] == "New conversation"

    async def test_create_accepts_and_tidies_a_title(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers, title="  spaced   out  ")
        assert conversation["title"] == "spaced out"

    async def test_rename_updates_the_title_and_bumps_the_sort_key(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers, title="Before")

        response = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            json={"title": "After"},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["title"] == "After"
        assert response.json()["updated_at"] > conversation["updated_at"]

    async def test_an_empty_rename_is_rejected_by_validation(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers)

        response = await client.patch(
            f"/api/v1/conversations/{conversation['id']}", json={"title": ""}, headers=headers
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"

    async def test_a_response_never_leaks_internal_columns(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers)
        # Serialized by a Pydantic model, not the ORM row: no user_id, no
        # deleted_at, nothing a client has no business knowing.
        assert set(conversation) == {"id", "title", "created_at", "updated_at"}


class TestSoftDeletion:
    async def test_a_deleted_conversation_is_gone_from_every_read_path(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers, title="Doomed")

        deleted = await client.delete(
            f"/api/v1/conversations/{conversation['id']}", headers=headers
        )
        assert deleted.status_code == 204

        direct = await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)
        assert direct.status_code == 404
        assert direct.json()["error"]["code"] == "conversation_not_found"

        listing = await client.get("/api/v1/conversations", headers=headers)
        assert listing.json()["items"] == []

        messages = await client.get(
            f"/api/v1/conversations/{conversation['id']}/messages", headers=headers
        )
        assert messages.status_code == 404

    async def test_deleting_twice_reports_not_found(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers)
        await client.delete(f"/api/v1/conversations/{conversation['id']}", headers=headers)

        again = await client.delete(f"/api/v1/conversations/{conversation['id']}", headers=headers)

        assert again.status_code == 404


class TestConversationPagination:
    async def test_every_conversation_is_returned_exactly_once(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        created = [await create_conversation(client, headers, title=f"c{i}") for i in range(5)]

        collected = await _walk(client, headers, limit=2)

        assert {row["id"] for row in collected} == {row["id"] for row in created}
        assert len(collected) == len(created), "a row was returned on two pages"

    async def test_pages_are_newest_first(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        for i in range(5):
            await create_conversation(client, headers, title=f"c{i}")

        collected = await _walk(client, headers, limit=2)

        stamps = [row["updated_at"] for row in collected]
        assert stamps == sorted(stamps, reverse=True)

    async def test_a_row_created_mid_listing_cannot_shift_the_next_page(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        """The failure offset pagination has and keyset pagination does not.

        With OFFSET, inserting a row above the current page pushes everything
        down and page two repeats the last item of page one. The cursor names
        the last row *seen*, so the insert is simply invisible to this walk.
        """
        original = [await create_conversation(client, headers, title=f"c{i}") for i in range(4)]

        first = await client.get("/api/v1/conversations?limit=2", headers=headers)
        page_one = first.json()

        # A new conversation sorts to the very top, above everything paged so far.
        await create_conversation(client, headers, title="inserted mid-walk")

        second = await client.get(
            f"/api/v1/conversations?limit=2&cursor={page_one['next_cursor']}", headers=headers
        )
        page_two = second.json()

        seen = [row["id"] for row in page_one["items"] + page_two["items"]]
        assert len(seen) == len(set(seen)), "keyset pagination repeated a row"
        assert set(seen) == {row["id"] for row in original}

    async def test_next_cursor_is_null_on_the_final_page(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        await create_conversation(client, headers)
        await create_conversation(client, headers)

        response = await client.get("/api/v1/conversations?limit=2", headers=headers)

        # Exactly a full page with nothing beyond it must still terminate.
        assert len(response.json()["items"]) == 2
        assert response.json()["next_cursor"] is None
        assert response.json()["has_more"] is False

    @pytest.mark.parametrize("limit", [0, -1, 101])
    async def test_limit_is_bounded(
        self, client: AsyncClient, headers: dict[str, str], limit: int
    ) -> None:
        response = await client.get(f"/api/v1/conversations?limit={limit}", headers=headers)
        assert response.status_code == 422

    async def test_a_malformed_cursor_is_a_client_error_not_a_silent_reset(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/conversations?cursor=not-a-cursor", headers=headers)

        # Falling back to page one would loop a paginating client forever.
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cursor"


class TestMessages:
    async def test_messages_come_back_in_reading_order(
        self, client: AsyncClient, app: FastAPI, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers)
        await seed_messages(
            app,
            user_email=USER_EMAIL,
            conversation_id=uuid.UUID(conversation["id"]),
            contents=[("user", "one"), ("assistant", "two"), ("user", "three")],
        )

        response = await client.get(
            f"/api/v1/conversations/{conversation['id']}/messages", headers=headers
        )

        items = response.json()["items"]
        assert [row["content"] for row in items] == ["one", "two", "three"]
        assert [row["sequence_number"] for row in items] == [1, 2, 3]

    async def test_paging_walks_backwards_through_history(
        self, client: AsyncClient, app: FastAPI, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers)
        await seed_messages(
            app,
            user_email=USER_EMAIL,
            conversation_id=uuid.UUID(conversation["id"]),
            contents=[("user", str(i)) for i in range(5)],
        )
        path = f"/api/v1/conversations/{conversation['id']}/messages"

        first = (await client.get(f"{path}?limit=2", headers=headers)).json()
        second = (
            await client.get(f"{path}?limit=2&before={first['next_cursor']}", headers=headers)
        ).json()

        # Newest page first, each page itself oldest-to-newest.
        assert [row["content"] for row in first["items"]] == ["3", "4"]
        assert [row["content"] for row in second["items"]] == ["1", "2"]

    async def test_system_and_tool_turns_are_never_serialized(
        self, client: AsyncClient, app: FastAPI, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers)
        await seed_messages(
            app,
            user_email=USER_EMAIL,
            conversation_id=uuid.UUID(conversation["id"]),
            contents=[
                ("system", "SECRET SYSTEM PROMPT"),
                ("user", "hello"),
                ("tool", '{"raw": "provider payload"}'),
                ("assistant", "hi"),
            ],
        )

        response = await client.get(
            f"/api/v1/conversations/{conversation['id']}/messages", headers=headers
        )

        contents = [row["content"] for row in response.json()["items"]]
        assert contents == ["hello", "hi"]
        assert "SECRET SYSTEM PROMPT" not in response.text
        assert "provider payload" not in response.text

    async def test_the_first_user_message_titles_the_conversation(
        self, client: AsyncClient, app: FastAPI, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers)
        await seed_messages(
            app,
            user_email=USER_EMAIL,
            conversation_id=uuid.UUID(conversation["id"]),
            contents=[("user", "What changed in the EU AI Act this year?")],
        )

        response = await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)

        assert response.json()["title"] == "What changed in the EU AI Act this year?"

    async def test_a_user_chosen_title_survives_the_first_message(
        self, client: AsyncClient, app: FastAPI, headers: dict[str, str]
    ) -> None:
        conversation = await create_conversation(client, headers, title="My own title")
        await seed_messages(
            app,
            user_email=USER_EMAIL,
            conversation_id=uuid.UUID(conversation["id"]),
            contents=[("user", "something else entirely")],
        )

        response = await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)

        assert response.json()["title"] == "My own title"
