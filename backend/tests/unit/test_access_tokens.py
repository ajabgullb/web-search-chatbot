"""Personal access token helpers and the MCP transcript renderer (ADR-0019)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.api.mcp.server import render_transcript
from app.core.logging import redact
from app.models.message import MessageStatus
from app.models.personal_access_token import TokenScope
from app.schemas.message import CitationRead, MessageRead, PublicMessageRole
from app.services.access_tokens import (
    DISPLAY_PREFIX_LENGTH,
    TOKEN_PREFIX,
    Principal,
    generate_access_token,
    is_access_token_shaped,
)


def test_tokens_are_prefixed_random_and_distinct() -> None:
    tokens = {generate_access_token() for _ in range(50)}

    assert len(tokens) == 50
    for token in tokens:
        assert token.startswith(TOKEN_PREFIX)
        # 32 random bytes, base64url without padding.
        assert len(token) == len(TOKEN_PREFIX) + 43
        assert is_access_token_shaped(token)
    assert len(TOKEN_PREFIX) + 4 == DISPLAY_PREFIX_LENGTH


def test_only_our_token_shape_reaches_the_database() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    for value in ["", TOKEN_PREFIX, jwt, "sk-abc", TOKEN_PREFIX + "x" * 200]:
        assert not is_access_token_shaped(value), value


def test_a_token_is_redacted_wherever_it_appears_in_a_log_line() -> None:
    token = generate_access_token()

    assert token not in redact(f"client sent {token} today")
    assert token not in redact(f"Authorization: Bearer {token}")


def test_a_principal_allows_exactly_its_scopes() -> None:
    principal = Principal(
        user_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        scopes=frozenset({TokenScope.conversations_read}),
    )

    assert principal.allows(TokenScope.conversations_read)
    assert not principal.allows(TokenScope.runs_create)
    assert len(principal.user_ref) == 16


def _message(role: str, content: str, citations: list[CitationRead] | None = None) -> MessageRead:
    return MessageRead(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role=PublicMessageRole(role),
        content=content,
        status=MessageStatus.complete,
        sequence_number=1,
        created_at=datetime(2026, 11, 7, 9, 30, tzinfo=UTC),
        citations=citations or [],
    )


def test_a_transcript_lists_each_answers_sources_under_it() -> None:
    web = CitationRead(
        ordinal=1,
        source_id="S1",
        url="https://news.test/a",
        display_url="news.test/a",
        title="News",
        publisher=None,
        snippet="ignored in the transcript",
        published_at=None,
        retrieved_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    document = web.model_copy(
        update={"source_id": "D1", "kind": "document", "url": None, "page": 4, "title": "r.pdf"}
    )

    text = render_transcript(
        "Plans",
        [_message("user", "Where?"), _message("assistant", "Here [S1][D1].", [web, document])],
        truncated=True,
    )

    assert text.startswith("# Plans\n\n_Showing the latest 2 messages._\n")
    assert "## User · 2026-11-07 09:30 UTC\n\nWhere?" in text
    assert "- [S1] News https://news.test/a" in text
    assert "- [D1] r.pdf page 4" in text
    assert "ignored in the transcript" not in text
    assert text.endswith("\n")
