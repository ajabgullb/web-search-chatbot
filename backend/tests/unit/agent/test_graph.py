"""End-to-end graph behaviour, driven entirely by fake providers.

These tests run the real compiled graph. They assert the transitions, the
budgets, and the shape of the result — never the wording of an answer, which
belongs to the model and not to us.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agent.context import AgentLimits
from app.agent.nodes import NO_SOURCES_NOTICE, SEARCH_UNAVAILABLE_NOTICE
from app.agent.routing import SearchDecision
from app.providers.llm import LLMPermanentError, LLMTransientError
from app.providers.search import SearchPermanentError, SearchTransientError
from tests.fakes import FakeLLM, FakeSearchProvider, hit

from .conftest import DEFAULT_LIMITS

Run = Callable[..., Any]


def limits(**overrides: Any) -> AgentLimits:
    base = {
        "max_searches_per_run": DEFAULT_LIMITS.max_searches_per_run,
        "max_results_per_search": DEFAULT_LIMITS.max_results_per_search,
        "search_timeout_seconds": DEFAULT_LIMITS.search_timeout_seconds,
        "run_timeout_seconds": DEFAULT_LIMITS.run_timeout_seconds,
        "max_output_tokens": DEFAULT_LIMITS.max_output_tokens,
        "max_context_messages": DEFAULT_LIMITS.max_context_messages,
    }
    base.update(overrides)
    return AgentLimits(**base)


class TestDirectBranch:
    async def test_a_greeting_never_reaches_the_search_provider(self, run: Run) -> None:
        search = FakeSearchProvider()
        result = await run("hello", llm=FakeLLM(answers=["Hi there."]), search=search)

        assert result.searched is False
        assert result.search_count == 0
        assert search.queries == []
        assert result.answer == "Hi there."
        assert result.citations == []
        assert result.succeeded

    async def test_a_direct_answer_cannot_carry_a_citation(self, run: Run) -> None:
        """Even when the model invents one, it must not reach the user."""
        llm = FakeLLM(answers=["The capital is Paris [S1]."])
        result = await run("what is the capital of France", llm=llm, search=FakeSearchProvider())

        assert "[S1]" not in result.answer
        assert result.citations == []


class TestSearchBranch:
    async def test_a_fresh_question_searches_and_cites(self, run: Run) -> None:
        search = FakeSearchProvider(
            default=[hit("https://news.test/a", title="The Report", snippet="Rates rose.")]
        )
        llm = FakeLLM(answers=["Rates rose this quarter [S1]."])
        result = await run("what is the latest news on rates", llm=llm, search=search)

        assert result.searched is True
        assert result.search_count == 1
        assert result.answer == "Rates rose this quarter [S1]."
        assert [c["source_id"] for c in result.citations] == ["S1"]
        assert result.citations[0]["url"] == "https://news.test/a"
        assert result.degraded is False

    async def test_the_model_decides_the_ambiguous_middle(self, run: Run) -> None:
        search = FakeSearchProvider(default=[hit("https://ref.test/raft")])
        llm = FakeLLM(
            answers=["Raft elects a leader by timeout [S1]."],
            decision=SearchDecision(
                should_search=True, rationale="niche technical detail", queries=["raft election"]
            ),
        )
        result = await run("how does raft handle leader election", llm=llm, search=search)

        assert llm.structured_calls, "the heuristics should have deferred to the model"
        assert search.queries == ["raft election"]
        assert result.searched is True

    async def test_sources_the_answer_ignores_are_not_cited(self, run: Run) -> None:
        search = FakeSearchProvider(default=[hit("https://a.test/1"), hit("https://b.test/2")])
        llm = FakeLLM(answers=["Only the first matters [S1]."])
        result = await run("latest figures", llm=llm, search=search)

        assert len(result.sources) == 2
        assert [c["source_id"] for c in result.citations] == ["S1"]


class TestBudgets:
    async def test_the_search_budget_bounds_the_run(self, run: Run) -> None:
        search = FakeSearchProvider(default=[hit("https://a.test/1")])
        llm = FakeLLM(
            answers=["Answer [S1]."],
            decision=SearchDecision(
                should_search=True,
                rationale="needs evidence",
                queries=["one", "two", "three"],
            ),
        )
        result = await run(
            "compare these three obscure standards",
            llm=llm,
            search=search,
            limits=limits(max_searches_per_run=2),
        )

        assert len(search.queries) == 2
        assert result.search_count == 2

    async def test_the_run_deadline_produces_a_retryable_failure(self, run: Run) -> None:
        """A run that overruns must end as a clean failure, not a hung request."""
        llm = FakeLLM(answers=["too late"], delay_seconds=0.5)
        result = await run(
            "hello", llm=llm, search=FakeSearchProvider(), limits=limits(run_timeout_seconds=0.05)
        )

        assert result.succeeded is False
        assert result.error is not None
        assert result.error["code"] == "run_timeout"
        assert result.error["retryable"] is True
        assert result.answer == ""


class TestDegradedPaths:
    async def test_a_search_outage_still_answers_and_says_so(self, run: Run) -> None:
        search = FakeSearchProvider(error=SearchTransientError("down"))
        llm = FakeLLM(answers=["From memory, rates were stable."])
        result = await run("what is the latest on rates", llm=llm, search=search)

        assert result.succeeded is True
        assert result.degraded is True
        assert result.notice == SEARCH_UNAVAILABLE_NOTICE
        assert result.citations == []
        assert result.answer

    async def test_a_bad_search_key_is_not_retried(self, run: Run) -> None:
        search = FakeSearchProvider(error=SearchPermanentError("unauthorised"))
        result = await run("latest news", llm=FakeLLM(answers=["No sources."]), search=search)

        assert len(search.queries) == 1
        assert result.degraded is True

    async def test_empty_results_route_away_from_synthesis(self, run: Run) -> None:
        """With nothing retrieved there is nothing to cite, so we answer directly."""
        search = FakeSearchProvider(default=[])
        llm = FakeLLM(answers=["I could not find anything specific."])
        result = await run("latest on an obscure topic", llm=llm, search=search)

        assert result.searched is True
        assert result.sources == []
        assert result.citations == []
        assert result.succeeded is True

    async def test_a_search_that_finds_nothing_is_flagged_unverified(self, run: Run) -> None:
        """AGENTS.md section 7: a searched answer with no valid citation is
        degraded and says so. Finding nothing is not the same as an outage,
        but the answer is just as unbacked, and must not look sourced."""
        search = FakeSearchProvider(default=[])
        llm = FakeLLM(answers=["It launched last week to strong reviews [S1]."])
        result = await run("latest news about the zorblax launch", llm=llm, search=search)

        assert result.degraded is True
        assert result.notice == NO_SOURCES_NOTICE
        assert "[S1]" not in result.answer

    async def test_a_searched_answer_with_no_valid_citation_is_flagged(self, run: Run) -> None:
        search = FakeSearchProvider(default=[hit("https://a.test/1")])
        llm = FakeLLM(answers=["Something substantial happened. " * 12])
        result = await run("latest news", llm=llm, search=search)

        assert result.degraded is True
        assert result.citations == []

    async def test_invented_citations_are_discarded(self, run: Run) -> None:
        search = FakeSearchProvider(default=[hit("https://a.test/1")])
        llm = FakeLLM(answers=["Real [S1]. Invented [S42]."])
        result = await run("latest news", llm=llm, search=search)

        assert "[S42]" not in result.answer
        assert [c["source_id"] for c in result.citations] == ["S1"]


class TestModelFailures:
    async def test_a_model_outage_is_a_retryable_failure(self, run: Run) -> None:
        llm = FakeLLM(answer_error=LLMTransientError("503"))
        result = await run("hello", llm=llm, search=FakeSearchProvider())

        assert result.succeeded is False
        assert result.error is not None
        assert result.error["code"] == "llm_unavailable"
        assert result.error["retryable"] is True

    async def test_a_permanent_model_error_is_not_retryable(self, run: Run) -> None:
        llm = FakeLLM(answer_error=LLMPermanentError("401"))
        result = await run("hello", llm=llm, search=FakeSearchProvider())

        assert result.error is not None
        assert result.error["retryable"] is False

    async def test_a_failed_decision_call_falls_back_to_answering(self, run: Run) -> None:
        """The classifier failing must not fail the run."""
        llm = FakeLLM(answers=["Here is what I know."], structured_error=LLMTransientError("503"))
        search = FakeSearchProvider()
        result = await run("how does raft handle leader election", llm=llm, search=search)

        assert result.succeeded is True
        assert result.searched is False
        assert search.queries == []


class TestResultMetadata:
    async def test_the_prompt_version_and_model_are_recorded(self, run: Run) -> None:
        result = await run("hello", llm=FakeLLM(model="fake-1"), search=FakeSearchProvider())

        assert result.model == "fake-1"
        assert result.prompt_version
        assert result.prompt_tokens == 11
        assert result.completion_tokens == 7

    async def test_the_rationale_is_recorded_for_logs(self, run: Run) -> None:
        result = await run("hello", llm=FakeLLM(), search=FakeSearchProvider())
        assert result.search_rationale.startswith("heuristic:")
