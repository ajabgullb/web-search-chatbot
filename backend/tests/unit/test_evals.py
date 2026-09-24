"""The deterministic eval subset, as part of the normal test run.

CI also runs `python -m evals.run --subset ci` for the printed report; this
makes a plain `make test` fail on the same regressions.
"""

from __future__ import annotations

from evals.harness import load_dataset
from evals.metrics import Prices, gate_failures
from evals.run import evaluate

# AGENTS.md section 13: the categories the dataset must cover.
REQUIRED_CATEGORIES = {
    "no_search",
    "freshness",
    "multi_source",
    "conflicting_sources",
    "prompt_injection",
    "search_outage",
    "empty_results",
    "malformed_citation",
    "conversation_follow_up",
}


def test_the_dataset_covers_every_required_category() -> None:
    categories = {case["category"] for case in load_dataset().cases}
    assert categories >= REQUIRED_CATEGORIES


def test_every_live_case_has_a_rubric_for_the_judge() -> None:
    for case in load_dataset().cases:
        if case.get("live") is not None:
            assert case["live"]["rubric"], case["id"]
            assert case["live"]["search"] in {"real", "scripted"}, case["id"]


async def test_the_ci_subset_passes_its_gates() -> None:
    version, outcomes, summary = await evaluate("ci", Prices())

    assert version
    assert gate_failures(summary, outcomes, "ci") == []
    assert summary["cases"] == len(load_dataset().cases)
