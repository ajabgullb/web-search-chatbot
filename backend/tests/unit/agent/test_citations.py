"""Citation validation: the rule that a rendered citation points at a real source."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.citations import extract_citations, is_degraded
from app.agent.state import Source

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def source(source_id: str, url: str = "https://example.com/a") -> Source:
    return Source(
        source_id=source_id,
        url=url,
        display_url="example.com/a",
        title=f"Title {source_id}",
        snippet="Evidence.",
        publisher=None,
        published_at=None,
        retrieved_at=NOW,
    )


class TestValidMarkers:
    def test_a_cited_source_becomes_a_citation(self) -> None:
        answer, citations, discarded = extract_citations("The sky is blue [S1].", [source("S1")])
        assert answer == "The sky is blue [S1]."
        assert [c["source_id"] for c in citations] == ["S1"]
        assert discarded == []

    def test_citations_are_ordered_by_first_use_not_by_label(self) -> None:
        sources = [source("S1"), source("S2", "https://example.com/b")]
        _, citations, _ = extract_citations("First [S2]. Then [S1]. Again [S2].", sources)
        assert [c["source_id"] for c in citations] == ["S2", "S1"]
        assert [c["ordinal"] for c in citations] == [1, 2]

    def test_a_source_cited_twice_appears_once(self) -> None:
        _, citations, _ = extract_citations("A [S1]. B [S1].", [source("S1")])
        assert len(citations) == 1

    def test_the_comma_form_is_split_into_separate_markers(self) -> None:
        sources = [source("S1"), source("S2", "https://example.com/b")]
        answer, citations, _ = extract_citations("Both agree [S1, S2].", sources)
        assert answer == "Both agree [S1][S2]."
        assert len(citations) == 2

    def test_an_uncited_source_is_not_a_citation(self) -> None:
        """Only sources the answer actually used are persisted."""
        sources = [source("S1"), source("S2", "https://example.com/b")]
        _, citations, _ = extract_citations("Only one [S1].", sources)
        assert [c["source_id"] for c in citations] == ["S1"]


class TestInvalidMarkers:
    def test_an_unknown_label_is_stripped_from_the_answer(self) -> None:
        answer, citations, discarded = extract_citations(
            "Real [S1]. Invented [S9].", [source("S1")]
        )
        assert "[S9]" not in answer
        assert "[S1]" in answer
        assert [c["source_id"] for c in citations] == ["S1"]
        assert discarded == ["S9"]

    def test_every_marker_is_stripped_when_there_are_no_sources(self) -> None:
        """The direct-answer path must not render a citation to nothing."""
        answer, citations, discarded = extract_citations("Confident [S1].", [])
        assert "[S1]" not in answer
        assert citations == []
        assert discarded == ["S1"]

    def test_a_partially_invalid_group_keeps_only_the_valid_half(self) -> None:
        answer, citations, _ = extract_citations("Mixed [S1, S7].", [source("S1")])
        assert answer == "Mixed [S1]."
        assert [c["source_id"] for c in citations] == ["S1"]

    def test_malformed_markers_are_left_alone(self) -> None:
        """[citation needed] is prose, not a broken citation."""
        answer, citations, discarded = extract_citations("Text [citation needed].", [source("S1")])
        assert answer == "Text [citation needed]."
        assert citations == []
        assert discarded == []


class TestDegradedDetection:
    def test_a_searched_answer_with_no_valid_citation_is_degraded(self) -> None:
        assert is_degraded(searched=True, answer="x" * 200, citations=[]) is True

    def test_a_cited_answer_is_not_degraded(self) -> None:
        _, citations, _ = extract_citations("Fact [S1].", [source("S1")])
        assert is_degraded(searched=True, answer="x" * 200, citations=citations) is False

    def test_an_unsearched_answer_is_never_degraded(self) -> None:
        """A direct answer was never supposed to have citations."""
        assert is_degraded(searched=False, answer="x" * 200, citations=[]) is False

    def test_a_short_reply_is_not_flagged(self) -> None:
        """A clarifying question has no factual claims to support."""
        assert is_degraded(searched=True, answer="Could you clarify?", citations=[]) is False
