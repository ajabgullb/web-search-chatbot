"""Optional LangSmith tracing: off by default, and content never uploaded."""

from __future__ import annotations

import os
import uuid
from typing import Any, ClassVar

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client
from langsmith import utils as langsmith_utils

from app.agent.graph import build_graph, run_agent
from app.agent.prompts import PROMPT_VERSION
from app.agent.routing import SearchDecision
from app.agent.tracing import GRAPH_VERSION, TraceConfig, Tracing, build_tracing, hide_payload
from app.core.config import Environment, Settings
from tests.fakes import FakeLLM, FakeSearchProvider, hit
from tests.unit.agent.conftest import make_context

QUESTION = "quokka-question-marker: what changed today?"
PAGE_TEXT = "pangolin-page-marker: something changed."
ANSWER = "axolotl-answer-marker: it changed [S1]."


class RecordingHandler(AsyncCallbackHandler):
    """Captures what LangChain hands a callback when the graph starts."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []

    async def on_chain_start(
        self, serialized: dict[str, Any] | None, inputs: Any, **kwargs: Any
    ) -> None:
        self.starts.append(kwargs)


def test_tracing_is_off_by_default() -> None:
    tracing = build_tracing(Settings(environment=Environment.test))

    trace = tracing.for_run(run_id=uuid.uuid4())

    assert tracing.enabled is False
    assert trace.callbacks == []
    # Tags and version metadata are still attached: free when nothing listens.
    assert trace.metadata["prompt_version"] == PROMPT_VERSION
    assert trace.metadata["graph_version"] == GRAPH_VERSION
    assert "env:test" in trace.tags


def test_an_environment_variable_cannot_switch_on_implicit_full_content_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LANGSMITH_TRACING in the container environment would make LangChain
    trace every call with full content, bypassing the hiding. The app owns it."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")

    build_tracing(Settings(environment=Environment.test, langsmith_tracing=False))

    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert langsmith_utils.tracing_is_enabled() is False


def test_enabled_tracing_attaches_a_tracer_that_hides_content() -> None:
    tracing = build_tracing(
        Settings(environment=Environment.test, langsmith_tracing=True, langsmith_api_key="lsv2_x")
    )

    trace = tracing.for_run(run_id=uuid.uuid4())

    assert tracing.enabled is True
    [tracer] = trace.callbacks
    assert isinstance(tracer, LangChainTracer)
    assert hide_payload({"messages": [QUESTION]}) == {}


def test_trace_metadata_names_the_run_but_not_the_user_or_conversation() -> None:
    run_id = uuid.uuid4()
    metadata = Tracing(None, project="p", environment="production").for_run(run_id=run_id).metadata

    assert metadata == {
        "environment": "production",
        "prompt_version": PROMPT_VERSION,
        "graph_version": GRAPH_VERSION,
        "run_id": str(run_id),
    }


async def test_the_graph_run_carries_the_tags_and_metadata() -> None:
    recorder = RecordingHandler()
    trace = Tracing(None, project="p", environment="test").for_run(run_id=uuid.uuid4())
    context = make_context(FakeLLM(answers=["Hello."]), FakeSearchProvider())
    context = type(context)(
        **{
            **{slot: getattr(context, slot) for slot in context.__slots__},
            "trace": TraceConfig(callbacks=[recorder], tags=trace.tags, metadata=trace.metadata),
        }
    )

    await run_agent(build_graph(), context=context, history=[HumanMessage(content="hi")])

    root = recorder.starts[0]
    assert root["name"] == "chat_run"
    assert "env:test" in root["tags"]
    assert root["metadata"]["prompt_version"] == PROMPT_VERSION
    assert root["metadata"]["graph_version"] == GRAPH_VERSION


async def test_nothing_uploaded_contains_the_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capture exactly what the LangSmith client would send over the wire for
    a searched, cited run, and look for the question, page text, and answer."""
    sent: list[bytes] = []

    class FakeResponse:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}
        text = "{}"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {}

    client = Client(
        api_key="lsv2_x",
        api_url="http://langsmith.invalid",
        hide_inputs=hide_payload,
        hide_outputs=hide_payload,
        auto_batch_tracing=False,
    )

    def capture(method: str, url: str, *args: Any, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("data") or kwargs.get("content") or b""
        sent.append(body if isinstance(body, bytes) else str(body).encode())
        return FakeResponse()

    monkeypatch.setattr(client.session, "request", capture)
    tracing = Tracing(client, project="p", environment="test")
    context = make_context(
        FakeLLM(
            answers=[ANSWER],
            decision=SearchDecision(should_search=True, rationale="fresh", queries=["changes"]),
        ),
        FakeSearchProvider(default=[hit("https://news.test/a", snippet=PAGE_TEXT)]),
    )
    context = type(context)(
        **{
            **{slot: getattr(context, slot) for slot in context.__slots__},
            "trace": tracing.for_run(run_id=uuid.uuid4()),
        }
    )

    result = await run_agent(
        build_graph(), context=context, history=[HumanMessage(content=QUESTION)]
    )
    for tracer in context.trace.callbacks if context.trace else []:
        assert isinstance(tracer, LangChainTracer)
        tracer.wait_for_futures()

    assert result.error is None
    uploaded = b"".join(sent)
    assert uploaded, "the tracer should have sent the run"
    assert PROMPT_VERSION.encode() in uploaded
    for marker in (b"quokka-question-marker", b"pangolin-page-marker", b"axolotl-answer-marker"):
        assert marker not in uploaded
