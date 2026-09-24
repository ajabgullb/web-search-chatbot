"""Aggregate metrics, quality gates, and the printed report."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any

from evals.harness import CaseOutcome, Subset

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_MARKER = re.compile(r"\[S\d+\]")


@dataclass(frozen=True)
class Prices:
    """US dollars per million tokens. Defaults: gpt-4o-mini list prices."""

    prompt: float = 0.15
    completion: float = 0.60


@dataclass(frozen=True)
class Gates:
    min_decision_accuracy: float
    min_citation_validity: float
    min_task_score: float
    max_unsupported_claim_rate: float


# CI is deterministic: anything short of perfect is a regression. Live runs
# are graded by a model and allowed some noise.
GATES: dict[Subset, Gates] = {
    "ci": Gates(1.0, 1.0, 1.0, 1.0),
    "live": Gates(0.8, 0.95, 0.7, 0.2),
}


def _ratio(numerator: float, denominator: float, *, empty: float = 1.0) -> float:
    return numerator / denominator if denominator else empty


def _uncited_sentence_rate(outcomes: list[CaseOutcome]) -> float:
    """Deterministic stand-in for unsupported claims: in answers that were
    meant to be sourced, the share of sentences carrying no citation."""
    total = uncited = 0
    for outcome in outcomes:
        result = outcome.result
        if not result.citations or result.degraded:
            continue
        sentences = [s for s in _SENTENCE.split(result.answer.strip()) if s]
        total += len(sentences)
        uncited += sum(1 for sentence in sentences if not _MARKER.search(sentence))
    return _ratio(uncited, total, empty=0.0)


def summarise(outcomes: list[CaseOutcome], *, subset: Subset, prices: Prices) -> dict[str, Any]:
    decisions = [o.decision_correct for o in outcomes if o.decision_correct is not None]
    citations = sum(len(o.result.citations) for o in outcomes)
    valid = sum(o.citations_valid for o in outcomes)
    emitted = sum(o.emitted_marker_ids for o in outcomes)
    kept = sum(o.valid_marker_ids for o in outcomes)
    latencies = sorted(o.latency_seconds for o in outcomes)
    prompt_tokens = sum(o.result.prompt_tokens or 0 for o in outcomes)
    completion_tokens = sum(o.result.completion_tokens or 0 for o in outcomes)

    judged = [o.judge for o in outcomes if o.judge is not None]
    if judged:
        task_score = statistics.mean(j["score"] for j in judged) / 5
        claims = sum(j["claims"] for j in judged)
        unsupported = _ratio(sum(j["unsupported_claims"] for j in judged), claims, empty=0.0)
        unsupported_method = "judge"
    else:
        task_score = _ratio(sum(o.passed for o in outcomes), len(outcomes))
        unsupported = _uncited_sentence_rate(outcomes)
        unsupported_method = "uncited-sentence proxy"

    return {
        "cases": len(outcomes),
        "cases_passed": sum(o.passed for o in outcomes),
        "search_decision_accuracy": _ratio(sum(decisions), len(decisions)),
        "decisions_scored": len(decisions),
        "citation_validity": _ratio(valid, citations),
        "citation_precision": _ratio(kept, emitted),
        "task_score": task_score,
        "task_score_method": "judge rubric / 5" if judged else "share of cases passing",
        "unsupported_claim_rate": unsupported,
        "unsupported_claim_method": unsupported_method,
        "latency_p50_seconds": statistics.median(latencies) if latencies else 0.0,
        "latency_p95_seconds": latencies[int(0.95 * (len(latencies) - 1))] if latencies else 0.0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "approx_cost_usd": round(
            (prompt_tokens * prices.prompt + completion_tokens * prices.completion) / 1_000_000, 6
        ),
        "cost_note": "fake token counts" if subset == "ci" else "provider-reported tokens",
    }


def gate_failures(
    summary: dict[str, Any], outcomes: list[CaseOutcome], subset: Subset
) -> list[str]:
    gates = GATES[subset]
    failures: list[str] = []
    if subset == "ci":
        failures += [
            f"{o.case_id}: {check.name} ({check.detail})"
            for o in outcomes
            for check in o.checks
            if not check.passed
        ]
    if summary["search_decision_accuracy"] < gates.min_decision_accuracy:
        failures.append(f"search_decision_accuracy {summary['search_decision_accuracy']:.2f}")
    if summary["citation_validity"] < gates.min_citation_validity:
        failures.append(f"citation_validity {summary['citation_validity']:.2f}")
    if summary["task_score"] < gates.min_task_score:
        failures.append(f"task_score {summary['task_score']:.2f}")
    if summary["unsupported_claim_rate"] > gates.max_unsupported_claim_rate:
        failures.append(f"unsupported_claim_rate {summary['unsupported_claim_rate']:.2f}")
    return failures


def render(
    outcomes: list[CaseOutcome], summary: dict[str, Any], version: str, subset: Subset
) -> str:
    lines = [f"Agent evals: dataset {version}, subset {subset}", ""]
    for outcome in outcomes:
        mark = "PASS" if outcome.passed else "FAIL"
        extra = f"  judge={outcome.judge['score']}/5" if outcome.judge else ""
        lines.append(
            f"  {mark}  {outcome.case_id:<26} {outcome.category:<22} "
            f"{outcome.latency_seconds * 1000:7.0f} ms{extra}"
        )
        lines.extend(
            f"          - {check.name}: {check.detail}"
            for check in outcome.checks
            if not check.passed
        )
    lines += [
        "",
        f"  search decision accuracy  {summary['search_decision_accuracy']:.2f}"
        f"  ({summary['decisions_scored']} scored)",
        f"  citation validity         {summary['citation_validity']:.2f}",
        f"  citation precision        {summary['citation_precision']:.2f}",
        f"  task score                {summary['task_score']:.2f}"
        f"  ({summary['task_score_method']})",
        f"  unsupported claim rate    {summary['unsupported_claim_rate']:.2f}"
        f"  ({summary['unsupported_claim_method']})",
        f"  latency p50 / p95         {summary['latency_p50_seconds']:.2f}s"
        f" / {summary['latency_p95_seconds']:.2f}s",
        f"  approx cost               ${summary['approx_cost_usd']:.4f}  ({summary['cost_note']})",
    ]
    return "\n".join(lines)
