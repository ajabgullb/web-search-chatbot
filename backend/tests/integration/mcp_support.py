"""Helpers for the MCP endpoint tests (ADR-0019).

The endpoint is stateless, so a test can send any JSON-RPC request on its
own, without an ``initialize`` first, exactly as a second replica would see
it. Responses come back as server-sent events (progress notifications, then
the result); httpx's ASGI transport collects the body whole, which is all
these tests need.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from httpx import AsyncClient, Response

MCP_URL = "/api/v1/mcp"
# A version that uses the initialize handshake, which current clients send.
PROTOCOL_VERSION = "2025-06-18"
ALL_SCOPES = ("conversations:read", "runs:create")


@dataclass
class McpReply:
    response: Response
    messages: list[dict[str, Any]]

    @property
    def status(self) -> int:
        return self.response.status_code

    @property
    def final(self) -> dict[str, Any]:
        """The JSON-RPC response (the last message: notifications come first)."""
        replies = [message for message in self.messages if "id" in message]
        assert len(replies) == 1, self.messages
        return replies[0]

    @property
    def result(self) -> dict[str, Any]:
        final = self.final
        assert "result" in final, final
        result: dict[str, Any] = final["result"]
        return result

    @property
    def structured(self) -> dict[str, Any]:
        """A successful tool call's structured content."""
        assert not self.result.get("isError"), self.result
        structured: dict[str, Any] = self.result["structuredContent"]
        return structured

    @property
    def tool_error(self) -> str:
        """A failed tool call's message."""
        assert self.result.get("isError") is True, self.result
        return "".join(part["text"] for part in self.result["content"])

    def notifications(self, method: str) -> list[dict[str, Any]]:
        return [message for message in self.messages if message.get("method") == method]


def parse_messages(response: Response) -> list[dict[str, Any]]:
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        body = response.json()
        return body if isinstance(body, list) else [body]
    messages = []
    # sse-starlette ends lines with CRLF; the SSE spec allows CR, LF, or both.
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        data = [
            line[len("data:") :].strip() for line in block.split("\n") if line.startswith("data:")
        ]
        if data and data[0]:
            messages.append(json.loads("\n".join(data)))
    return messages


async def mcp_post(
    client: AsyncClient,
    token: str | None,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> McpReply:
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        **({"Authorization": f"Bearer {token}"} if token is not None else {}),
        **(headers or {}),
    }
    response = await client.post(MCP_URL, json=payload, headers=request_headers)
    messages = parse_messages(response) if response.status_code < 300 else []
    return McpReply(response=response, messages=messages)


async def mcp_request(
    client: AsyncClient,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> McpReply:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    return await mcp_post(client, token, payload, headers=headers)


async def call_tool(
    client: AsyncClient,
    token: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    progress_token: str | None = None,
) -> McpReply:
    params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
    if progress_token is not None:
        params["_meta"] = {"progressToken": progress_token}
    return await mcp_request(client, token, "tools/call", params)


async def create_token(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "Test client",
    scopes: tuple[str, ...] = ALL_SCOPES,
    expires_in_days: int = 30,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/access-tokens",
        json={"name": name, "scopes": list(scopes), "expires_in_days": expires_in_days},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())
