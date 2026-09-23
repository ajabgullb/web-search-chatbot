"""Run request validation and idempotency fingerprints."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.runs import RunCreate
from app.services.runs import normalize_content, request_fingerprint

CONVERSATION = uuid.UUID("00000000-0000-0000-0000-00000000000a")


class TestRunCreate:
    def test_a_message_or_a_retry_but_not_both(self) -> None:
        RunCreate(content="hi")
        RunCreate(retry_message_id=uuid.uuid4())
        with pytest.raises(ValidationError):
            RunCreate()
        with pytest.raises(ValidationError):
            RunCreate(content="hi", retry_message_id=uuid.uuid4())


class TestNormalizeContent:
    def test_line_endings_are_unified_and_ends_trimmed(self) -> None:
        assert normalize_content("  a\r\nb\rc  ") == "a\nb\nc"

    def test_nul_is_removed(self) -> None:
        """PostgreSQL text cannot hold NUL; dropping it beats a 500."""
        assert normalize_content("a\x00b") == "ab"

    def test_inner_whitespace_is_preserved(self) -> None:
        assert normalize_content("keep   the\n\n  indentation") == "keep   the\n\n  indentation"


class TestFingerprint:
    def test_same_request_same_fingerprint(self) -> None:
        a = request_fingerprint(conversation_id=CONVERSATION, content="hi", retry_message_id=None)
        b = request_fingerprint(conversation_id=CONVERSATION, content="hi", retry_message_id=None)
        assert a == b
        assert len(a) == 64

    @pytest.mark.parametrize(
        "other",
        [
            {"conversation_id": uuid.uuid4(), "content": "hi", "retry_message_id": None},
            {"conversation_id": CONVERSATION, "content": "bye", "retry_message_id": None},
            {"conversation_id": CONVERSATION, "content": None, "retry_message_id": uuid.uuid4()},
        ],
    )
    def test_any_difference_changes_it(self, other: dict[str, object]) -> None:
        base = request_fingerprint(
            conversation_id=CONVERSATION, content="hi", retry_message_id=None
        )
        assert request_fingerprint(**other) != base  # type: ignore[arg-type]
