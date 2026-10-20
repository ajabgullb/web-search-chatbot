"""Which chunks an answer reads, and how document citations are validated.

Pure functions, so every rule is pinned exactly: the keyword extraction that
feeds full-text search, the fusion of the two rankings, the relevance floor,
the budget, and ``D`` labels in the citation validator and stream filter.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.citations import CitationStreamFilter, extract_citations
from app.agent.tools.documents import (
    exact_terms,
    fuse,
    keyword_query,
    keyword_terms,
    retrieval_query,
    select,
    to_sources,
)
from tests.fakes import chunk

NOW = datetime(2026, 10, 20, tzinfo=UTC)


class TestKeywords:
    def test_common_words_are_dropped_and_order_kept(self) -> None:
        assert keyword_terms("What was the revenue growth in the third quarter?") == [
            "revenue",
            "growth",
            "third",
            "quarter",
        ]

    def test_codes_and_numbers_survive(self) -> None:
        assert keyword_terms("Which batch failed: XR-7731 or 42?") == [
            "batch",
            "failed",
            "xr",
            "7731",
            "42",
        ]

    def test_duplicates_collapse_and_the_list_is_capped(self) -> None:
        terms = keyword_terms(" ".join(f"term{i} term{i}" for i in range(30)))
        assert len(terms) == 12
        assert len(set(terms)) == 12

    @pytest.mark.parametrize(
        "hostile", ["a & b | !c", "x:* (y)", "foo <-> bar", "'quoted' \\\\ back", "___"]
    )
    def test_text_search_operators_never_reach_the_query(self, hostile: str) -> None:
        """Only letters and digits survive, so to_tsquery sees no syntax."""
        query = keyword_query(keyword_terms(hostile))
        assert not set(query) & set("&!:*()<>'\\\\_")

    def test_terms_are_joined_with_or(self) -> None:
        assert keyword_query(["revenue", "growth"]) == "revenue | growth"


class TestFusion:
    def test_agreement_between_the_two_lists_wins(self) -> None:
        a, b, c = chunk("a", similarity=0.9), chunk("b", similarity=0.8), chunk("c", similarity=0.3)
        ranked = fuse([a, b], [c, b])
        assert [r.candidate.content for r in ranked] == ["b", "a", "c"]
        assert [r.keyword_match for r in ranked] == [True, False, True]

    def test_the_order_is_deterministic_on_ties(self) -> None:
        a, b = chunk("a", similarity=0.5), chunk("b", similarity=0.7)
        first = [r.candidate.content for r in fuse([a], [b])]
        assert first == [r.candidate.content for r in fuse([a], [b])] == ["b", "a"]


class TestSelection:
    def test_below_the_floor_without_a_keyword_match_is_dropped(self) -> None:
        relevant, noise = chunk("relevant", similarity=0.5), chunk("noise", similarity=0.1)
        chosen = select(fuse([relevant, noise], []), min_similarity=0.25, top_k=6, max_chars=10_000)
        assert [r.candidate.content for r in chosen] == ["relevant"]

    def test_an_exact_match_on_a_code_is_kept_below_the_floor(self) -> None:
        """Part numbers are what the keyword half is for; embeddings blur them."""
        code = chunk("Part XR-7731 failed", similarity=0.05)
        exact = exact_terms(keyword_terms("what happened to XR-7731"))
        chosen = select(
            fuse([], [code]), min_similarity=0.25, top_k=6, max_chars=10_000, exact=exact
        )
        assert exact == ["7731"]
        assert [r.candidate.content for r in chosen] == ["Part XR-7731 failed"]

    def test_a_shared_ordinary_word_does_not_bypass_the_floor(self) -> None:
        """'board' in 'who sits on the board' matched a dividend passage in the
        retrieval eval. A plain word helps rank, never relevance."""
        dividend = chunk("The board proposes a final dividend.", similarity=0.2)
        exact = exact_terms(keyword_terms("Who sits on the board of directors?"))
        chosen = select(
            fuse([dividend], [dividend]),
            min_similarity=0.25,
            top_k=6,
            max_chars=10_000,
            exact=exact,
        )
        assert chosen == []

    def test_nothing_relevant_means_nothing_chosen(self) -> None:
        noise = [chunk(f"n{i}", similarity=0.05) for i in range(5)]
        assert select(fuse(noise, []), min_similarity=0.25, top_k=6, max_chars=10_000) == []

    def test_top_k_and_the_character_budget_both_bound_the_selection(self) -> None:
        many = [chunk("x" * 100, similarity=0.9 - i / 100) for i in range(10)]
        assert len(select(fuse(many, []), min_similarity=0.0, top_k=3, max_chars=10_000)) == 3
        assert len(select(fuse(many, []), min_similarity=0.0, top_k=10, max_chars=250)) == 2

    def test_the_best_chunk_fits_even_over_budget(self) -> None:
        big = chunk("y" * 5_000, similarity=0.9)
        assert len(select(fuse([big], []), min_similarity=0.0, top_k=6, max_chars=1_000)) == 1


class TestSources:
    def test_chunks_become_d_labelled_document_sources(self) -> None:
        a, b = chunk("first", page=2), chunk("second", filename="notes.pdf", page=7)
        sources = to_sources(fuse([a, b], []), retrieved_at=NOW)

        assert [s["source_id"] for s in sources] == ["D1", "D2"]
        assert [(s["title"], s.get("page")) for s in sources] == [
            ("report.pdf", 2),
            ("notes.pdf", 7),
        ]
        assert all(s.get("kind") == "document" and s["url"] is None for s in sources)
        assert sources[0].get("chunk_id") == str(a.chunk_id)

    def test_a_short_follow_up_borrows_the_previous_question(self) -> None:
        assert (
            retrieval_query("and costs?", ["What was revenue?"]) == "What was revenue? and costs?"
        )
        long = "What did the report say about costs in the third quarter?"
        assert retrieval_query(long, ["What was revenue?"]) == long
        assert retrieval_query("and costs?", []) == "and costs?"


class TestDocumentCitations:
    def sources(self) -> list:  # type: ignore[type-arg]
        return to_sources(fuse([chunk("a", page=3), chunk("b", page=4)], []), retrieved_at=NOW)

    def test_d_labels_validate_and_carry_the_page(self) -> None:
        _, citations, discarded = extract_citations("Grew [D2] and [D1].", self.sources())
        assert [c["source_id"] for c in citations] == ["D2", "D1"]
        assert [c.get("page") for c in citations] == [4, 3]
        assert discarded == []

    def test_mixed_brackets_are_split_and_unknown_labels_dropped(self) -> None:
        answer, _, discarded = extract_citations("See [D1, S4, D9].", self.sources())
        assert answer == "See [D1]."
        assert discarded == ["S4", "D9"]

    def test_a_d_marker_split_across_chunks_is_held_then_resolved(self) -> None:
        stream = CitationStreamFilter(self.sources())
        first = stream.feed("Revenue grew [")
        second = stream.feed("D")
        third = stream.feed("1] strongly.")
        assert first == ("Revenue grew ", [])
        assert second == ("", [])
        text, citations = third
        assert text == "[D1] strongly."
        assert [c["source_id"] for c in citations] == ["D1"]
