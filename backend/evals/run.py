"""Run the agent evaluations.

    uv run python -m evals.run --subset ci      # deterministic, free, gated in CI
    uv run python -m evals.run --subset live    # real model + judge; costs money

Exits non-zero when a quality gate fails. Each run also writes a JSON report to
``evals/reports/`` (git-ignored) for comparing runs over time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.context import AgentLimits
from app.core.config import get_settings
from app.providers.llm import build_llm_provider
from app.providers.search import build_search_provider
from evals.harness import (
    CaseOutcome,
    Subset,
    cases_for,
    load_dataset,
    run_case,
    scripted_llm,
    scripted_search,
)
from evals.judge import judge
from evals.metrics import Prices, gate_failures, render, summarise

REPORTS_DIR = Path(__file__).with_name("reports")


async def evaluate(subset: Subset, prices: Prices) -> tuple[str, list[CaseOutcome], dict[str, Any]]:
    dataset = load_dataset()
    settings = get_settings()
    limits = AgentLimits.from_settings(settings)
    outcomes: list[CaseOutcome] = []

    if subset == "ci":
        for case in cases_for(dataset, "ci"):
            outcomes.append(
                await run_case(
                    case,
                    llm=scripted_llm(case),
                    search=scripted_search(case),
                    limits=limits,
                    subset="ci",
                )
            )
        return dataset.version, outcomes, summarise(outcomes, subset="ci", prices=prices)

    if not settings.llm_api_key or not settings.search_api_key:
        raise SystemExit("The live subset needs LLM_API_KEY and SEARCH_API_KEY.")
    llm = build_llm_provider(settings)
    real_search = build_search_provider(settings)
    try:
        for case in cases_for(dataset, "live"):
            search = real_search if case["live"]["search"] == "real" else scripted_search(case)
            outcome = await run_case(case, llm=llm, search=search, limits=limits, subset="live")
            outcome.judge = await judge(llm, case=case, result=outcome.result)
            outcomes.append(outcome)
    finally:
        await real_search.aclose()
    return dataset.version, outcomes, summarise(outcomes, subset="live", prices=prices)


def _write_report(
    version: str, subset: Subset, outcomes: list[CaseOutcome], summary: dict[str, Any]
) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"{stamp}-{subset}.json"
    # Identifiers, scores, and check results only: reports are shared, and
    # live answers can be long and are not needed to compare runs.
    report = {
        "dataset_version": version,
        "subset": subset,
        "summary": summary,
        "cases": [
            {
                "id": o.case_id,
                "category": o.category,
                "passed": o.passed,
                "latency_seconds": round(o.latency_seconds, 3),
                "searched": o.result.searched,
                "citations": len(o.result.citations),
                "degraded": o.result.degraded,
                "failed_checks": [c.name for c in o.checks if not c.passed],
                "judge": o.judge,
            }
            for o in outcomes
        ],
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subset", choices=["ci", "live"], default="ci")
    parser.add_argument("--price-prompt", type=float, default=Prices.prompt, help="USD / 1M tokens")
    parser.add_argument(
        "--price-completion", type=float, default=Prices.completion, help="USD / 1M tokens"
    )
    parser.add_argument("--no-report", action="store_true", help="skip writing the JSON report")
    args = parser.parse_args(argv)
    subset: Subset = args.subset

    prices = Prices(prompt=args.price_prompt, completion=args.price_completion)
    version, outcomes, summary = asyncio.run(evaluate(subset, prices))
    print(render(outcomes, summary, version, subset))
    if not args.no_report:
        print(f"\n  report: {_write_report(version, subset, outcomes, summary)}")

    failures = gate_failures(summary, outcomes, subset)
    if failures:
        print("\nGATE FAILED:\n" + "\n".join(f"  - {failure}" for failure in failures))
        return 1
    print("\nAll gates passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
