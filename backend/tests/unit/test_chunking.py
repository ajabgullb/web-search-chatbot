"""Page text into chunks: pure, so tested exhaustively without I/O."""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.services.chunking import chunk_pages, normalize_page_text
from app.services.documents import sanitize_filename

TARGET = 200
OVERLAP = 40


def chunk(pages: list[str]) -> list[tuple[int, str]]:
    return [
        (c.page, c.content) for c in chunk_pages(pages, target_chars=TARGET, overlap_chars=OVERLAP)
    ]


class TestNormalization:
    def test_ragged_spacing_is_tidied_but_paragraphs_survive(self) -> None:
        raw = "  First   line \r\nsecond\tline\n\n\n\nNew  paragraph  "
        assert normalize_page_text(raw) == "First line\nsecond line\n\nNew paragraph"

    def test_characters_postgresql_cannot_store_are_removed(self) -> None:
        """NUL fails a text insert outright; a lone surrogate fails UTF-8 encoding."""
        cleaned = normalize_page_text("a\x00b\ud800c\x07d")
        assert "\x00" not in cleaned
        cleaned.encode("utf-8")  # would raise on a surviving surrogate

    def test_a_page_of_only_whitespace_is_empty(self) -> None:
        assert normalize_page_text(" \n\t \n ") == ""


class TestChunking:
    def test_a_short_page_is_one_chunk(self) -> None:
        assert chunk(["Revenue grew."]) == [(1, "Revenue grew.")]

    def test_chunks_never_span_pages_and_carry_their_page(self) -> None:
        assert chunk(["first page", "", "third page"]) == [(1, "first page"), (3, "third page")]

    def test_no_chunk_exceeds_the_target_plus_overlap(self) -> None:
        text = " ".join(f"word{i}" for i in range(2_000))
        sizes = [len(content) for _, content in chunk([text])]
        assert len(sizes) > 5
        assert max(sizes) <= TARGET + OVERLAP

    def test_paragraph_boundaries_are_preferred(self) -> None:
        """Each paragraph fits on its own, so no chunk should cut through one."""
        paragraphs = [f"Paragraph {i}. " + "x " * 40 for i in range(6)]
        for _, content in chunk(["\n\n".join(paragraphs)]):
            starts = [p.strip()[:12] for p in paragraphs if p.strip()[:12] in content]
            assert starts, content

    def test_consecutive_chunks_overlap(self) -> None:
        """A fact that straddles a boundary is whole in at least one chunk."""
        text = " ".join(f"w{i:04d}" for i in range(400))
        chunks = [content for _, content in chunk([text])]
        for before, after in pairwise(chunks):
            first_word = after.split()[0]
            assert first_word in before

    def test_text_with_no_spaces_is_still_cut(self) -> None:
        chunks = chunk(["x" * (TARGET * 3)])
        assert len(chunks) >= 3
        assert all(len(content) <= TARGET + OVERLAP for _, content in chunks)

    def test_nothing_is_lost(self) -> None:
        """Every word of the page appears in some chunk."""
        words = [f"token{i}" for i in range(500)]
        joined = " ".join(content for _, content in chunk([" ".join(words)]))
        assert all(word in joined for word in words)

    def test_overlap_must_be_smaller_than_the_chunk(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            chunk_pages(["text"], target_chars=100, overlap_chars=100)


class TestFilenames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("report.pdf", "report.pdf"),
            ("C:\\Users\\alice\\Documents\\report.pdf", "report.pdf"),
            ("/home/alice/report.pdf", "report.pdf"),
            ("bad\nname\r\x00.pdf", "bad name .pdf"),
            ("  spaced   out .pdf ", "spaced out .pdf"),
            ("", "document.pdf"),
            (None, "document.pdf"),
            ("\x00\x01", "document.pdf"),
        ],
    )
    def test_names_are_reduced_to_a_safe_display_name(self, raw: str | None, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    def test_a_long_name_keeps_its_extension(self) -> None:
        name = sanitize_filename("a" * 400 + ".pdf")
        assert len(name) == 255
        assert name.endswith(".pdf")
