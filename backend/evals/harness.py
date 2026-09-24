"""Running one evaluation case through the real agent graph.

Both subsets drive the same compiled graph the API runs. Only the providers
differ: scripted fakes in ``ci`` (deterministic, free), and the configured
model, plus real or scripted search, in ``live``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from app.agent.context import AgentContext, AgentLimits
from app.agent.graph import AgentResult, build_graph, run_agent
from app.agent.prompts import ANSWER_DIRECT_SYSTEM, SEARCH_DECISION_SYSTEM, SYNTHESIZE_SYSTEM
from app.agent.routing import SearchDecision
from app.providers.llm import LLMChunk, LLMProvider, StructuredT
from app.providers.search import SearchHit, SearchPermanentError, SearchProvider
from tests.fakes import FakeLLM, FakeSearchProvider

DATASET_PATH = Path(__file__).with_name("dataset.json")
Subset = Literal["ci", "live"]

_MARKER = re.compile(r"\[(S\d+)\]")


@dataclass(frozen=True)
class Dataset:
    version: str
    cases: list[dict[str, Any]]


def load_dataset(path: Path = DATASET_PATH) -> Dataset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    ids = [case["id"] for case in raw["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("case ids must be unique")
    return Dataset(version=raw["version"], cases=raw["cases"])


def cases_for(dataset: Dataset, subset: Subset) -> list[dict[str, Any]]:
    if subset == "ci":
        return dataset.cases
    return [case for case in dataset.cases if case.get("live") is not None]


class RecordingLLM:
    """Passes calls through and keeps what the model actually emitted.

    The finished answer has invalid citation markers stripped, so citation
    precision (valid markers over emitted markers) needs the raw text.
    """

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.emitted: list[str] = []
        self.prompts: list[list[AnyMessage]] = []

    @property
    def model(self) -> str:
        return self._inner.model

    async def stream(
        self, messages: Sequence[AnyMessage], *, max_output_tokens: int | None = None
    ) -> AsyncIterator[LLMChunk]:
        self.prompts.append(list(messages))
        parts: list[str] = []
        try:
            async for chunk in self._inner.stream(messages, max_output_tokens=max_output_tokens):
                parts.append(chunk.text)
                yield chunk
        finally:
            self.emitted.append("".join(parts))

    async def structured(
        self, messages: Sequence[AnyMessage], schema: type[StructuredT]
    ) -> StructuredT:
        return await self._inner.structured(messages, schema)


class RecordingSearch:
    def __init__(self, inner: SearchProvider) -> None:
        self._inner = inner
        self.queries: list[str] = []
        self.returned_urls: set[str] = set()

    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        self.queries.append(query)
        hits = await self._inner.search(query, max_results=max_results)
        self.returned_urls.update(hit.url for hit in hits)
        return hits

    async def aclose(self) -> None:
        await self._inner.aclose()


def scripted_llm(case: dict[str, Any]) -> LLMProvider:
    script = case["script"]
    decision = script.get("decision")
    return FakeLLM(
        answers=[script["answer"]],
        decision=SearchDecision(**decision) if decision else None,
    )


def scripted_search(case: dict[str, Any]) -> SearchProvider:
    script = case["script"]
    if script.get("search_error"):
        return FakeSearchProvider(error=SearchPermanentError("scripted outage"))
    hits = [SearchHit(**result) for result in script.get("results", [])]
    return FakeSearchProvider(default=hits)


def history_for(case: dict[str, Any]) -> list[AnyMessage]:
    turns: list[AnyMessage] = []
    for role, content in case.get("history", []):
        turns.append(
            HumanMessage(content=content) if role == "user" else AIMessage(content=content)
        )
    turns.append(HumanMessage(content=case["question"]))
    return turns


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseOutcome:
    case_id: str
    category: str
    result: AgentResult
    emitted_text: str
    queries: list[str]
    latency_seconds: float
    checks: list[Check] = field(default_factory=list)
    decision_correct: bool | None = None
    citations_valid: int = 0
    emitted_marker_ids: int = 0
    valid_marker_ids: int = 0
    judge: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def _system_prompt_fragments() -> list[str]:
    """Sentences long enough that seeing one in an answer means a leak."""
    fragments: list[str] = []
    for prompt in (SYNTHESIZE_SYSTEM, ANSWER_DIRECT_SYSTEM, SEARCH_DECISION_SYSTEM):
        fragments.extend(
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+|\n", prompt)
            if len(sentence.strip()) >= 40
        )
    return fragments


_PROMPT_FRAGMENTS = _system_prompt_fragments()


async def run_case(
    case: dict[str, Any],
    *,
    llm: LLMProvider,
    search: SearchProvider,
    limits: AgentLimits,
    subset: Subset,
) -> CaseOutcome:
    recording_llm = RecordingLLM(llm)
    recording_search = RecordingSearch(search)
    context = AgentContext(
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        llm=recording_llm,
        search=recording_search,
        limits=limits,
        request_id=f"eval-{case['id']}",
    )
    started = time.perf_counter()
    result = await run_agent(build_graph(), context=context, history=history_for(case))
    latency = time.perf_counter() - started

    outcome = CaseOutcome(
        case_id=case["id"],
        category=case["category"],
        result=result,
        emitted_text="\n".join(recording_llm.emitted),
        queries=list(recording_search.queries),
        latency_seconds=latency,
    )
    _evaluate(case, outcome, recording_llm, recording_search, subset)
    return outcome


def _evaluate(
    case: dict[str, Any],
    outcome: CaseOutcome,
    llm: RecordingLLM,
    search: RecordingSearch,
    subset: Subset,
) -> None:
    expect = case["expect"]
    result = outcome.result
    checks = outcome.checks

    checks.append(Check("no_error", result.error is None, str(result.error or "")))

    # Search decision. In the CI subset a model decision is scripted, so it is
    # only counted where the heuristics are what decides.
    if expect.get("search") is not None:
        correct = result.searched is expect["search"]
        checks.append(Check("search_decision", correct, f"searched={result.searched}"))
        if subset == "live" or expect.get("decided_by") == "heuristic":
            outcome.decision_correct = correct
        if subset == "ci" and expect.get("decided_by") == "heuristic":
            by_heuristic = result.search_rationale.startswith("heuristic:")
            checks.append(Check("decided_by_heuristic", by_heuristic, result.search_rationale))

    # Citation validity: every citation names a source this run retrieved.
    source_ids = {source["source_id"] for source in result.sources}
    valid = [
        citation
        for citation in result.citations
        if citation["source_id"] in source_ids and citation["url"] in search.returned_urls
    ]
    outcome.citations_valid = len(valid)
    checks.append(
        Check(
            "citations_valid",
            len(valid) == len(result.citations),
            f"{len(valid)}/{len(result.citations)}",
        )
    )

    # Precision: of the source labels the model emitted, how many survived.
    emitted_ids = set(_MARKER.findall(outcome.emitted_text))
    outcome.emitted_marker_ids = len(emitted_ids)
    outcome.valid_marker_ids = len(emitted_ids & {c["source_id"] for c in valid})

    count = len(result.citations)
    if "min_citations" in expect:
        checks.append(Check("min_citations", count >= expect["min_citations"], f"{count}"))
    if "max_citations" in expect:
        checks.append(Check("max_citations", count <= expect["max_citations"], f"{count}"))
    if "degraded" in expect:
        checks.append(
            Check("degraded", result.degraded is expect["degraded"], str(result.degraded))
        )
    if expect.get("notice"):
        checks.append(Check("notice_shown", bool(result.notice), result.notice[:60]))
    if "queries_exactly" in expect:
        checks.append(
            Check(
                "queries_exactly", search.queries == expect["queries_exactly"], str(search.queries)
            )
        )
    for forbidden in expect.get("answer_must_not_contain", []):
        checks.append(Check(f"answer_excludes:{forbidden}", forbidden not in result.answer, ""))
    # The prompt checks inspect what the model was shown. Only meaningful
    # where the prompt is ours to inspect, which is always, but the scripted
    # answer never depends on it, so these are the context-use checks in CI.
    prompt_text = "\n".join(str(m.content) for prompt in llm.prompts for m in prompt)
    for required in expect.get("prompt_must_contain", []):
        checks.append(Check(f"prompt_includes:{required}", required in prompt_text, ""))

    leaked = [fragment for fragment in _PROMPT_FRAGMENTS if fragment in result.answer]
    checks.append(Check("no_system_prompt_leak", not leaked, leaked[0][:60] if leaked else ""))
    checks.append(
        Check(
            "no_claim_of_search_without_search",
            result.searched or "I searched" not in result.answer,
            "",
        )
    )
