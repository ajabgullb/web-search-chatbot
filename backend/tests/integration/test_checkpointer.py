"""LangGraph's PostgreSQL checkpointer against a real database.

Two things matter here. A thread must come back with its earlier turns, because
that is what makes a conversation a conversation after a restart. And a
checkpoint must contain no client and no credential, because it is long-lived
storage that outlives the request that wrote it.
"""

from __future__ import annotations

import uuid

import pytest

from app.agent.checkpointer import checkpointer_for, psycopg_dsn
from app.agent.context import AgentContext, AgentLimits
from app.agent.graph import build_graph, run_agent
from app.core.config import Settings
from tests.fakes import FakeLLM, FakeSearchProvider, hit

pytestmark = pytest.mark.integration

LIMITS = AgentLimits(
    max_searches_per_run=3,
    max_results_per_search=5,
    search_timeout_seconds=20.0,
    run_timeout_seconds=90.0,
    max_output_tokens=1_500,
    max_context_messages=20,
)

SECRET_KEY = "tvly-super-secret-key"  # a fake key, asserted absent below


def context_for(
    conversation_id: uuid.UUID, llm: FakeLLM, search: FakeSearchProvider
) -> AgentContext:
    return AgentContext(
        user_id=uuid.uuid4(),
        conversation_id=conversation_id,
        run_id=uuid.uuid4(),
        llm=llm,
        search=search,
        limits=LIMITS,
    )


def test_the_dsn_is_translated_for_psycopg() -> None:
    """The app's engine speaks asyncpg; the checkpointer speaks psycopg."""
    assert (
        psycopg_dsn("postgresql+asyncpg://u:p@localhost:5432/app")
        == "postgresql://u:p@localhost:5432/app"
    )


async def test_a_thread_resumes_with_its_earlier_turns(settings: Settings) -> None:
    conversation_id = uuid.uuid4()
    search = FakeSearchProvider()

    async with checkpointer_for(settings) as saver:
        graph = build_graph(saver)

        first = await run_agent(
            graph,
            context=context_for(conversation_id, FakeLLM(answers=["Hello there."]), search),
            user_message="hello",
        )
        assert first.succeeded

        # A second run on the same thread, through a fresh context, exactly as
        # the next HTTP request would arrive.
        llm = FakeLLM(answers=["You said hello."])
        second = await run_agent(
            graph,
            context=context_for(conversation_id, llm, search),
            user_message="what did I just say?",
        )
        assert second.succeeded

        # The resumed run saw the earlier turns, not just the new message.
        prompt = llm.last_prompt_text
        assert "hello" in prompt
        assert "Hello there." in prompt

        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": str(conversation_id)}}  # type: ignore[arg-type]
        )
        roles = [type(message).__name__ for message in snapshot.values["messages"]]
        assert roles == ["HumanMessage", "AIMessage", "HumanMessage", "AIMessage"]


async def test_threads_are_isolated_from_each_other(settings: Settings) -> None:
    """A conversation id is the thread id, so two conversations never mix."""
    search = FakeSearchProvider()
    async with checkpointer_for(settings) as saver:
        graph = build_graph(saver)

        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        await run_agent(
            graph,
            context=context_for(first_id, FakeLLM(answers=["One."]), search),
            user_message="first conversation",
        )
        llm = FakeLLM(answers=["Two."])
        await run_agent(
            graph,
            context=context_for(second_id, llm, search),
            user_message="second conversation",
        )

        assert "first conversation" not in llm.last_prompt_text

        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": str(second_id)}}  # type: ignore[arg-type]
        )
        assert len(snapshot.values["messages"]) == 2


async def test_no_client_or_credential_is_written_to_a_checkpoint(settings: Settings) -> None:
    """Context carries the clients precisely so that state does not."""
    conversation_id = uuid.uuid4()
    search = FakeSearchProvider(default=[hit("https://a.test/1")])

    async with checkpointer_for(settings) as saver:
        graph = build_graph(saver)
        await run_agent(
            graph,
            context=context_for(conversation_id, FakeLLM(answers=["Answer [S1]."]), search),
            user_message="what is the latest news",
        )

        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": str(conversation_id)}}  # type: ignore[arg-type]
        )
        values = snapshot.values
        assert "llm" not in values
        assert "search" not in values
        assert SECRET_KEY not in repr(values)
        # Sources survive the round trip as plain data.
        assert values["sources"][0]["source_id"] == "S1"
        assert values["sources"][0]["retrieved_at"].tzinfo is not None
