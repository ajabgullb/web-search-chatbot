"""Title derivation and cursor encoding — pure functions, no database."""

from __future__ import annotations

import pytest

from app.core.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.services.conversations import (
    DEFAULT_TITLE,
    TITLE_MAX_LENGTH,
    derive_title,
    normalize_title,
)


class TestDeriveTitle:
    def test_a_short_question_is_used_verbatim(self) -> None:
        assert derive_title("Who won the 2026 World Cup?") == "Who won the 2026 World Cup?"

    def test_whitespace_and_newlines_collapse_to_one_line(self) -> None:
        assert derive_title("  What   is\n\nthis?\t") == "What is this?"

    def test_an_empty_message_falls_back_rather_than_producing_an_empty_title(self) -> None:
        assert derive_title("   \n\t  ") == DEFAULT_TITLE

    def test_a_long_message_is_cut_at_a_word_boundary(self) -> None:
        content = (
            "Explain in detail how photosynthesis converts light energy "
            "into chemical energy inside a plant cell"
        )

        title = derive_title(content)

        assert len(title) <= TITLE_MAX_LENGTH
        assert title.endswith("…")
        # Cut between words, not through one.
        assert not title.removesuffix("…").endswith(" ")
        assert content.startswith(title.removesuffix("…"))

    def test_a_single_enormous_word_is_still_truncated(self) -> None:
        title = derive_title("x" * 500)

        assert len(title) <= TITLE_MAX_LENGTH
        assert title.endswith("…")

    def test_it_is_deterministic(self) -> None:
        # No model call, so the same input must always give the same title.
        assert derive_title("a question") == derive_title("a question")


class TestNormalizeTitle:
    def test_it_collapses_whitespace(self) -> None:
        assert normalize_title("  many   spaces  ") == "many spaces"

    def test_a_whitespace_only_title_becomes_the_default(self) -> None:
        assert normalize_title("   ") == DEFAULT_TITLE


class TestCursorCodec:
    def test_a_cursor_round_trips(self) -> None:
        assert decode_cursor(encode_cursor("2026-01-01T00:00:00+00:00", "abc"), fields=2) == (
            "2026-01-01T00:00:00+00:00",
            "abc",
        )

    def test_it_is_url_safe_and_unpadded(self) -> None:
        cursor = encode_cursor("2026-09-06T21:00:00.123456+00:00", "f" * 36)

        assert "=" not in cursor
        assert "+" not in cursor and "/" not in cursor

    @pytest.mark.parametrize("bad", ["not base64!", "", "YWJj"])
    def test_garbage_raises_rather_than_silently_returning_page_one(self, bad: str) -> None:
        with pytest.raises(InvalidCursorError):
            decode_cursor(bad, fields=2)

    def test_the_wrong_field_count_is_rejected(self) -> None:
        cursor = encode_cursor("one")
        with pytest.raises(InvalidCursorError):
            decode_cursor(cursor, fields=2)
