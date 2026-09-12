"""The search step: budgets, retries, normalisation, and outage behaviour."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.context import AgentLimits
from app.agent.tools.search import execute_searches, normalize_hits
from app.providers.search import SearchPermanentError, SearchTransientError
from tests.fakes import FakeSearchProvider, hit

from .conftest import DEFAULT_LIMITS


def limits(**overrides: object) -> AgentLimits:
    values = {
        "max_searches_per_run": DEFAULT_LIMITS.max_searches_per_run,
        "max_results_per_search": DEFAULT_LIMITS.max_results_per_search,
        "search_timeout_seconds": DEFAULT_LIMITS.search_timeout_seconds,
        "run_timeout_seconds": DEFAULT_LIMITS.run_timeout_seconds,
        "max_output_tokens": DEFAULT_LIMITS.max_output_tokens,
        "max_context_messages": DEFAULT_LIMITS.max_context_messages,
    }
    values.update(overrides)
    return AgentLimits(**values)  # type: ignore[arg-type]


class TestNormalisation:
    def test_sources_are_labelled_in_order(self) -> None:
        sources = normalize_hits(
            [hit("https://a.test/1"), hit("https://b.test/2")],
            seen_keys=set(),
            next_ordinal=1,
            retrieved_at=datetime.now(UTC),
            limit=5,
        )
        assert [s["source_id"] for s in sources] == ["S1", "S2"]

    def test_duplicate_pages_collapse_to_one_source(self) -> None:
        sources = normalize_hits(
            [
                hit("https://example.com/a?utm_source=news"),
                hit("https://www.example.com/a"),
            ],
            seen_keys=set(),
            next_ordinal=1,
            retrieved_at=datetime.now(UTC),
            limit=5,
        )
        assert len(sources) == 1

    def test_an_unsafe_url_is_dropped_rather_than_rendered(self) -> None:
        sources = normalize_hits(
            [hit("javascript:alert(1)"), hit("https://ok.test/a")],
            seen_keys=set(),
            next_ordinal=1,
            retrieved_at=datetime.now(UTC),
            limit=5,
        )
        assert [s["url"] for s in sources] == ["https://ok.test/a"]

    def test_snippets_are_capped(self) -> None:
        sources = normalize_hits(
            [hit("https://a.test/1", snippet="x" * 5_000)],
            seen_keys=set(),
            next_ordinal=1,
            retrieved_at=datetime.now(UTC),
            limit=5,
        )
        assert len(sources[0]["snippet"]) == 1_200


class TestBudgets:
    async def test_the_search_count_is_a_hard_cap(self) -> None:
        """More queries than the budget allows must not mean more calls."""
        provider = FakeSearchProvider(default=[hit("https://a.test/1")])
        outcome = await execute_searches(
            [f"query {i}" for i in range(10)],
            provider=provider,
            limits=limits(max_searches_per_run=2),
        )
        assert len(provider.queries) == 2
        assert outcome.searches_run == 2

    async def test_results_per_search_are_capped(self) -> None:
        provider = FakeSearchProvider(default=[hit(f"https://a.test/{i}") for i in range(20)])
        outcome = await execute_searches(
            ["one"], provider=provider, limits=limits(max_results_per_search=3)
        )
        assert len(outcome.sources) == 3

    async def test_sources_are_deduplicated_across_separate_searches(self) -> None:
        provider = FakeSearchProvider(
            results={
                "one": [hit("https://same.test/a")],
                "two": [hit("https://same.test/a?utm_campaign=x")],
            }
        )
        outcome = await execute_searches(["one", "two"], provider=provider, limits=limits())
        assert len(outcome.sources) == 1
        assert outcome.searches_run == 2


class TestFailureHandling:
    async def test_a_transient_failure_is_retried_once_and_can_succeed(self) -> None:
        provider = FakeSearchProvider(
            default=[hit("https://a.test/1")],
            error=SearchTransientError("blip"),
            fail_times=1,
        )
        outcome = await execute_searches(["one"], provider=provider, limits=limits())
        assert len(provider.queries) == 2
        assert len(outcome.sources) == 1
        assert outcome.provider_failed is False

    async def test_a_transient_failure_is_retried_at_most_once(self) -> None:
        provider = FakeSearchProvider(error=SearchTransientError("down"))
        outcome = await execute_searches(["one"], provider=provider, limits=limits())
        assert len(provider.queries) == 2
        assert outcome.provider_failed is True

    async def test_a_permanent_failure_is_never_retried(self) -> None:
        """Retrying a bad key or a rejected query pays for the same error twice."""
        provider = FakeSearchProvider(error=SearchPermanentError("bad key"))
        outcome = await execute_searches(["one"], provider=provider, limits=limits())
        assert len(provider.queries) == 1
        assert outcome.provider_failed is True

    async def test_a_slow_provider_hits_the_per_search_timeout(self) -> None:
        provider = FakeSearchProvider(default=[hit("https://a.test/1")], delay_seconds=0.3)
        outcome = await execute_searches(
            ["one"], provider=provider, limits=limits(search_timeout_seconds=0.05)
        )
        assert outcome.provider_failed is True

    async def test_one_failed_query_does_not_discard_the_others(self) -> None:
        provider = FakeSearchProvider(
            results={"good": [hit("https://a.test/1")]},
            error=SearchTransientError("blip"),
            fail_times=2,
        )
        outcome = await execute_searches(["bad", "good"], provider=provider, limits=limits())
        assert outcome.provider_failed is False
        assert len(outcome.sources) == 1

    async def test_no_queries_means_no_provider_call(self) -> None:
        provider = FakeSearchProvider()
        outcome = await execute_searches([" ", ""], provider=provider, limits=limits())
        assert provider.queries == []
        assert outcome.sources == []
        assert outcome.provider_failed is False

    @pytest.mark.parametrize("empty", [[], [hit("javascript:alert(1)")]])
    async def test_an_empty_result_set_is_not_a_failure(self, empty: list[object]) -> None:
        """Searching and finding nothing is a legitimate outcome, not an outage."""
        provider = FakeSearchProvider(default=empty)  # type: ignore[arg-type]
        outcome = await execute_searches(["one"], provider=provider, limits=limits())
        assert outcome.sources == []
        assert outcome.provider_failed is False
