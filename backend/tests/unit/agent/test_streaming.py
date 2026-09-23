"""What the graph reports while it runs.

These drive ``stream_agent`` directly — no HTTP — to pin down the internal
event sequence the service layer maps onto SSE.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from app.agent.events import CitationAdded, SearchCompleted, SearchStarted, StatusChanged, TextDelta
from app.agent.graph import AgentResult, build_graph, stream_agent
from app.providers.search import SearchTransientError
from tests.fakes import FakeLLM, FakeSearchProvider, hit
from tests.unit.agent.conftest import make_context


async def _collect(question: str, llm: FakeLLM, search: FakeSearchProvider) -> list[object]:
    items: list[object] = []
    async for item in stream_agent(
        build_graph(), context=make_context(llm, search), history=[HumanMessage(question)]
    ):
        items.append(item)
    return items


def _kinds(items: list[object]) -> list[str]:
    out: list[str] = []
    for item in items:
        name = type(item).__name__
        if out and out[-1] == name == "TextDelta":
            continue  # collapse runs of deltas for readability
        out.append(name)
    return out


async def test_a_searched_answer_reports_every_phase_in_order() -> None:
    llm = FakeLLM(answers=["Rates are 5% [S1]."])
    search = FakeSearchProvider(default=[hit("https://a.test/1")])

    items = await _collect("latest rates?", llm, search)

    assert _kinds(items) == [
        "StatusChanged",  # thinking
        "StatusChanged",  # searching
        "SearchStarted",
        "SearchCompleted",
        "StatusChanged",  # writing
        "TextDelta",
        "CitationAdded",
        "TextDelta",
        "AgentResult",
    ]
    statuses = [i.status for i in items if isinstance(i, StatusChanged)]
    assert statuses == ["thinking", "searching", "writing"]
    (started,) = [i for i in items if isinstance(i, SearchStarted)]
    assert started.query == "latest rates?"
    (completed,) = [i for i in items if isinstance(i, SearchCompleted)]
    assert [s["source_id"] for s in completed.sources] == ["S1"]
    assert not completed.failed


async def test_streamed_text_and_citations_match_the_final_result() -> None:
    llm = FakeLLM(answers=["One [S2]. Two [S1][S9]. Three [S2]."])
    search = FakeSearchProvider(default=[hit("https://a.test/1"), hit("https://b.test/2")])

    items = await _collect("latest news?", llm, search)
    result = items[-1]
    assert isinstance(result, AgentResult)

    streamed = "".join(i.text for i in items if isinstance(i, TextDelta))
    cited = [i.citation for i in items if isinstance(i, CitationAdded)]
    assert cited == result.citations
    assert "[S9]" not in streamed
    # Identical up to the whitespace tidy the saved answer gets.
    assert " ".join(streamed.split()) == " ".join(result.answer.split())


async def test_a_failed_search_is_reported_as_failed() -> None:
    llm = FakeLLM(answers=["From memory."])
    search = FakeSearchProvider(error=SearchTransientError("down"))

    items = await _collect("latest news?", llm, search)

    completed = [i for i in items if isinstance(i, SearchCompleted)]
    assert completed and all(c.failed and c.sources == () for c in completed)


async def test_a_direct_answer_never_reports_searching() -> None:
    items = await _collect("hello", FakeLLM(answers=["Hi!"]), FakeSearchProvider())
    statuses = [i.status for i in items if isinstance(i, StatusChanged)]
    assert statuses == ["thinking", "writing"]
    assert not any(isinstance(i, SearchStarted) for i in items)


@pytest.mark.parametrize("answer", ["", "x"])
async def test_the_result_is_always_last_and_unique(answer: str) -> None:
    items = await _collect("hello", FakeLLM(answers=[answer]), FakeSearchProvider())
    results = [i for i in items if isinstance(i, AgentResult)]
    assert len(results) == 1
    assert items[-1] is results[0]
