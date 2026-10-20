"""Splitting page text into chunks for embedding and citation.

Pure functions, no I/O. The rules (ADR-0015):

- **A chunk never spans a page**, so every citation names exactly one page.
- **Natural boundaries first.** A page is split on paragraphs, then lines, then
  sentences, then words, and only as a last resort mid-word. A chunk that
  ends mid-sentence embeds worse and reads worse when quoted.
- **Overlap.** Each chunk after the first on a page starts with the end of the
  one before, so a fact that straddles a boundary is whole in at least one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Around 500 tokens of English. Small enough that a retrieved chunk is about
# one thing; large enough to carry its own context.
TARGET_CHARS = 2_000
OVERLAP_CHARS = 200

# Coarsest first. Each keeps its separator, so joining the pieces of a split
# gives back the original text.
_SEPARATORS = ("\n\n", "\n", ". ", " ")

# Control characters other than newline and tab. NUL in particular cannot be
# stored in a PostgreSQL text column at all.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class TextChunk:
    page: int  # 1-based
    content: str


def normalize_page_text(text: str) -> str:
    """Clean extracted text without changing what it says.

    PDF extraction yields ragged spacing, stray control characters, and
    occasionally lone surrogates, which PostgreSQL would reject. Paragraph
    breaks are kept: they are the best split points there are.
    """
    text = text.encode("utf-8", "replace").decode("utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub(" ", text)
    text = "\n".join(" ".join(line.split()) for line in text.split("\n"))
    return _EXCESS_BLANK_LINES.sub("\n\n", text).strip()


def _split(text: str, limit: int, level: int = 0) -> list[str]:
    """Pieces of at most ``limit`` characters, cut at the coarsest boundary that works."""
    if len(text) <= limit:
        return [text]
    if level == len(_SEPARATORS):
        return [text[i : i + limit] for i in range(0, len(text), limit)]

    separator = _SEPARATORS[level]
    parts = text.split(separator)
    pieces: list[str] = []
    for index, part in enumerate(parts):
        piece = part + separator if index < len(parts) - 1 else part
        if piece:
            pieces.extend(_split(piece, limit, level + 1))
    return pieces


def _tail(text: str, size: int) -> str:
    """The last ``size`` characters, starting at a word boundary if there is one."""
    if size <= 0 or not text:
        return ""
    if len(text) <= size:
        return text
    tail = text[-size:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


def _chunk_page(text: str, *, target: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for piece in _split(text, target):
        if current and len(current) + len(piece) > target:
            chunks.append(current.strip())
            current = _tail(current, overlap)
        current += piece
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def chunk_pages(
    pages: list[str], *, target_chars: int = TARGET_CHARS, overlap_chars: int = OVERLAP_CHARS
) -> list[TextChunk]:
    """Chunks for every page with text, in reading order.

    No chunk is longer than ``target_chars + overlap_chars``. Pages with no
    text (a full-page image, a blank separator page) produce no chunks.
    """
    if overlap_chars >= target_chars:
        raise ValueError("overlap must be smaller than the chunk size")
    chunks: list[TextChunk] = []
    for number, raw in enumerate(pages, start=1):
        text = normalize_page_text(raw)
        if text:
            chunks.extend(
                TextChunk(page=number, content=content)
                for content in _chunk_page(text, target=target_chars, overlap=overlap_chars)
            )
    return chunks
