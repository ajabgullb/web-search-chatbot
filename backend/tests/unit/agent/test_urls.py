"""URL canonicalisation, deduplication, and display.

These rules decide what can be cited and what counts as the same page, so they
are pinned tightly: a regression here either renders an unsafe link or shows
one article three times.
"""

from __future__ import annotations

import pytest

from app.agent.tools.search import (
    build_display_url,
    canonicalize_url,
    dedupe_key,
    validate_queries,
    validate_query,
)


class TestCanonicalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://Example.COM/Path", "https://example.com/Path"),
            ("https://example.com:443/a", "https://example.com/a"),
            ("http://example.com:80/a", "http://example.com/a"),
            ("https://example.com/a/", "https://example.com/a"),
            ("https://example.com", "https://example.com/"),
            ("https://example.com/a#section", "https://example.com/a"),
            ("https://example.com/a?utm_source=x&id=7", "https://example.com/a?id=7"),
            ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
            ("  https://example.com/a  ", "https://example.com/a"),
        ],
    )
    def test_canonical_forms(self, raw: str, expected: str) -> None:
        assert canonicalize_url(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "not a url",
            "",
            "https://",
            "https://example.com/a\nb",
        ],
    )
    def test_unsafe_or_unusable_urls_are_rejected(self, raw: str) -> None:
        """Only http and https may ever reach a renderer (AGENTS.md §6)."""
        assert canonicalize_url(raw) is None

    def test_port_is_kept_when_it_is_not_the_default(self) -> None:
        assert canonicalize_url("https://example.com:8443/a") == "https://example.com:8443/a"


class TestDeduplication:
    def test_www_and_scheme_variants_are_one_page(self) -> None:
        a = canonicalize_url("https://www.example.com/article")
        b = canonicalize_url("http://example.com/article")
        assert a is not None and b is not None
        assert dedupe_key(a) == dedupe_key(b)

    def test_different_paths_stay_distinct(self) -> None:
        a = canonicalize_url("https://example.com/one")
        b = canonicalize_url("https://example.com/two")
        assert a is not None and b is not None
        assert dedupe_key(a) != dedupe_key(b)

    def test_query_is_part_of_identity(self) -> None:
        a = canonicalize_url("https://example.com/p?page=1")
        b = canonicalize_url("https://example.com/p?page=2")
        assert a is not None and b is not None
        assert dedupe_key(a) != dedupe_key(b)


class TestDisplayUrl:
    def test_drops_scheme_and_www(self) -> None:
        assert build_display_url("https://www.example.com/a/b") == "example.com/a/b"

    def test_root_path_is_omitted(self) -> None:
        assert build_display_url("https://example.com/") == "example.com"

    def test_long_urls_are_truncated(self) -> None:
        shown = build_display_url("https://example.com/" + "x" * 300)
        assert len(shown) <= 80
        assert shown.endswith("…")


class TestQueryValidation:
    def test_whitespace_is_collapsed(self) -> None:
        assert validate_query("  a   b \n c ") == "a b c"

    def test_empty_queries_are_rejected(self) -> None:
        assert validate_query("   ") is None
        assert validate_query("") is None

    def test_overlong_queries_are_capped(self) -> None:
        cleaned = validate_query("q" * 5_000)
        assert cleaned is not None
        assert len(cleaned) == 400

    def test_duplicates_are_dropped_case_insensitively(self) -> None:
        assert validate_queries(["Rust 2026", "rust 2026", "go"], limit=5) == ["Rust 2026", "go"]

    def test_the_limit_is_a_hard_cap(self) -> None:
        assert validate_queries([f"q{i}" for i in range(20)], limit=3) == ["q0", "q1", "q2"]
