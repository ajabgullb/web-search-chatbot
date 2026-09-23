"""Streaming citation validation: the screen and the saved message agree.

The central property: however a model's output is cut into chunks, the
filter's output carries the same valid markers, in the same order, as the
batch validator run over the whole text — and never an invalid one.
"""

from __future__ import annotations

import random

import pytest

from app.agent.citations import CitationStreamFilter, extract_citations, strip_citation_markers
from app.agent.state import Citation
from tests.unit.agent.test_citations import source

SOURCES = [source("S1"), source("S2", "https://example.com/b"), source("S3", "https://c.test/")]

ANSWERS = [
    "Plain text with no markers at all.",
    "Rates rose [S1]. A pause is expected [S2].",
    "Grouped [S1, S2] and invented [S9] and mixed [S3, S7].",
    "Brackets that are not markers: [1], [note], [S], a[b]c, and [S1234].",
    "An unclosed marker at the very end [S2",
    "Lower case [s1] and spaced [ S2 ] forms.",
    "Repeated [S1][S1] citations count once [S1].",
    "Multibyte text — café [S1] 東京 [S2] 🚀 [S3].",
]


def _feed(chunks: list[str]) -> tuple[str, list[Citation]]:
    stream = CitationStreamFilter(SOURCES)
    text: list[str] = []
    citations: list[Citation] = []
    for chunk in chunks:
        out, new = stream.feed(chunk)
        text.append(out)
        citations.extend(new)
    out, new = stream.flush()
    text.append(out)
    citations.extend(new)
    return "".join(text), citations


def _random_chunks(text: str, rng: random.Random) -> list[str]:
    chunks, position = [], 0
    while position < len(text):
        size = rng.randint(1, 6)
        chunks.append(text[position : position + size])
        position += size
    return chunks


@pytest.mark.parametrize("answer", ANSWERS)
def test_any_chunking_agrees_with_the_batch_validator(answer: str) -> None:
    whole_text, whole_citations = _feed([answer])
    _, batch_citations, _ = extract_citations(answer, SOURCES)
    assert whole_citations == batch_citations

    rng = random.Random(answer)  # noqa: S311 - chunk boundaries, not security
    for _ in range(50):
        text, citations = _feed(_random_chunks(answer, rng))
        assert text == whole_text
        assert citations == batch_citations


@pytest.mark.parametrize("answer", ANSWERS)
def test_an_invented_label_never_reaches_the_output(answer: str) -> None:
    text, _ = _feed(list(answer))  # one character at a time: the worst case
    assert "[S9]" not in text
    assert "S7" not in text


def test_a_split_marker_is_held_back_then_released_whole() -> None:
    stream = CitationStreamFilter(SOURCES)
    assert stream.feed("Rates rose [") == ("Rates rose ", [])
    assert stream.feed("S") == ("", [])
    text, citations = stream.feed("1] and fell.")
    assert text == "[S1] and fell."
    assert [c["source_id"] for c in citations] == ["S1"]


def test_each_source_is_reported_once_in_first_use_order() -> None:
    _, citations = _feed(["[S2] then [S1] then [S2] again"])
    assert [(c["ordinal"], c["source_id"]) for c in citations] == [(1, "S2"), (2, "S1")]


def test_ordinary_brackets_are_not_held_back() -> None:
    stream = CitationStreamFilter(SOURCES)
    assert stream.feed("see [note] here") == ("see [note] here", [])


def test_a_runaway_bracket_does_not_stall_the_stream() -> None:
    """A '[' followed by a long run of text that still looks marker-ish is
    released once it exceeds the plausible length of a marker."""
    stream = CitationStreamFilter(SOURCES)
    released, _ = stream.feed("[S1, S2, S3, S1, S2, S3, S1, S2, S3, S1, S2, S3, S1")
    assert released.startswith("[")


def test_with_no_sources_every_marker_is_dropped() -> None:
    stream = CitationStreamFilter([])
    out, new = stream.feed("Claimed [S1] and [S2].")
    assert out == "Claimed  and ."
    assert new == []


def test_stripping_markers_from_history() -> None:
    assert strip_citation_markers("Paris [S1] is big [S2, S3].") == "Paris is big."
    assert strip_citation_markers("No markers [1] here.") == "No markers [1] here."
