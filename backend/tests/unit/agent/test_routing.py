"""Search-decision heuristics and the graph's routing functions.

The positive and negative cases named in AGENTS.md §7 are pinned here, because
"when do we search" is both the cost lever and the correctness lever of the
whole agent.
"""

from __future__ import annotations

import pytest

from app.agent.routing import heuristic_search_decision, route_after_decision, route_after_search


class TestDoesNotSearch:
    @pytest.mark.parametrize(
        "message",
        ["hi", "Hello!", "thanks", "Good morning", "how are you", "bye"],
    )
    def test_greetings_and_small_talk(self, message: str) -> None:
        assert heuristic_search_decision(message).should_search is False

    @pytest.mark.parametrize(
        "message",
        [
            "Rewrite this paragraph to be shorter.",
            "Translate the following into French.",
            "Summarize this for me.",
            "Fix the grammar in my email.",
            "Reformat that as a bullet list.",
        ],
    )
    def test_work_on_text_the_user_supplied(self, message: str) -> None:
        assert heuristic_search_decision(message).should_search is False

    def test_an_empty_message(self) -> None:
        assert heuristic_search_decision("   ").should_search is False


class TestSearches:
    @pytest.mark.parametrize(
        "message",
        [
            "What is the latest version of Python?",
            "Who won the match yesterday?",
            "What is the current price of bitcoin?",
            "Any news about the merger?",
            "What changed in the 2026 tax rules?",
            "What is the weather in Lahore?",
        ],
    )
    def test_questions_whose_answer_changes_over_time(self, message: str) -> None:
        assert heuristic_search_decision(message).should_search is True

    @pytest.mark.parametrize(
        "message",
        [
            "Explain quantum tunnelling with sources.",
            "Cite your sources for that claim.",
            "Can you give me references for this?",
            "Back this up please.",
        ],
    )
    def test_an_explicit_request_for_evidence(self, message: str) -> None:
        assert heuristic_search_decision(message).should_search is True


class TestDefersToTheModel:
    @pytest.mark.parametrize(
        "message",
        [
            "How does the Raft consensus algorithm handle leader election?",
            "Is it worth learning Haskell before Scala?",
            "What were the main causes of the 1873 panic?",
        ],
    )
    def test_ambiguous_questions_are_not_decided_by_rule(self, message: str) -> None:
        """The heuristics abstain rather than guess; the model decides."""
        assert heuristic_search_decision(message).should_search is None

    def test_a_greeting_prefix_does_not_suppress_a_real_question(self) -> None:
        """'hi, what is the current price' is a question, not a greeting."""
        decision = heuristic_search_decision("hi, what is the current price of gold?")
        assert decision.should_search is True


class TestRouting:
    def test_the_decision_picks_the_branch(self) -> None:
        assert route_after_decision(True) == "web_search"
        assert route_after_decision(False) == "answer_direct"

    def test_an_empty_result_set_avoids_synthesis(self) -> None:
        """Synthesizing from nothing is how fabricated citations happen."""
        assert route_after_search(0) == "answer_direct"
        assert route_after_search(1) == "synthesize"
