"""Request body size limit, enforced before a body is parsed."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.config import Settings
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import install_providers, messages_for

pytestmark = pytest.mark.integration


async def test_a_declared_oversized_body_is_refused_unread(
    client: AsyncClient, settings: Settings
) -> None:
    body = (
        b'{"email": "a@example.com", "password": "' + b"x" * settings.max_request_body_bytes + b'"}'
    )

    response = await client.post(
        "/api/v1/auth/login", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "payload_too_large"
    assert error["details"] == {"max_bytes": settings.max_request_body_bytes}
    assert error["request_id"]


async def test_an_undeclared_streamed_body_is_cut_off_at_the_limit(
    app: FastAPI, client: AsyncClient, settings: Settings
) -> None:
    """Chunked upload, no Content-Length: counted as it arrives."""
    await install_providers(app)
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    chunk = b"x" * 16_384
    chunks_needed = settings.max_request_body_bytes // len(chunk) + 2

    async def chunked() -> AsyncIterator[bytes]:
        yield b'{"content": "'
        for _ in range(chunks_needed):
            yield chunk
        yield b'"}'

    response = await client.post(
        f"/api/v1/conversations/{conversation['id']}/runs",
        content=chunked(),
        headers={**headers, "Content-Type": "application/json", "Idempotency-Key": "k-1"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert await messages_for(app, conversation["id"]) == []


async def test_a_maximum_length_message_fits_under_the_limit(
    app: FastAPI, client: AsyncClient, settings: Settings
) -> None:
    """The limit must never refuse a message the product allows. Worst case:
    every character a non-BMP emoji, ASCII-escaped by the client as a 12-byte
    surrogate pair."""
    await install_providers(app)
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    body = json.dumps({"content": "😀" * settings.max_user_message_chars}, ensure_ascii=True)
    assert len(body) > 16_000 * 12

    response = await client.post(
        f"/api/v1/conversations/{conversation['id']}/runs",
        content=body,
        headers={**headers, "Content-Type": "application/json", "Idempotency-Key": "k-2"},
    )

    assert response.status_code == 200
