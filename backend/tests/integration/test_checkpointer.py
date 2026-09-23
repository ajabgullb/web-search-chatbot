"""LangGraph's PostgreSQL checkpointer against a real database.

Three things matter here. The checkpoint must agree with the history the
application hands the graph — no duplicated turn after a retry, no orphaned
question after a failure — because the ``messages`` table is the canonical
history and the checkpoint mirrors it. Threads must never mix. And a checkpoint
must contain no client and no credential, because it is long-lived storage
that outlives the request that wrote it.
"""

from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.checkpointer import checkpointer_for, psycopg_dsn
from app.agent.context import AgentContext, AgentLimits
from app.agent.graph import build_graph, run_agent
from app.core.config import Settings
from app.providers.llm import LLMTransientError
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


def _config(conversation_id: uuid.UUID) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": str(conversation_id)}}


async def test_a_thread_continues_from_the_history_it_is_given(settings: Settings) -> None:
    conversation_id = uuid.uuid4()
    search = FakeSearchProvider()
    question = HumanMessage(content="hello", id="m1")

    async with checkpointer_for(settings) as saver:
        graph = build_graph(saver)

        first = await run_agent(
            graph,
            context=context_for(conversation_id, FakeLLM(answers=["Hello there."]), search),
            history=[question],
        )
        assert first.succeeded

        # The next request arrives with the history the database recorded,
        # through a fresh context, exactly as the run executor supplies it.
        llm = FakeLLM(answers=["You said hello."])
        second = await run_agent(
            graph,
            context=context_for(conversation_id, llm, search),
            history=[
                question,
                AIMessage(content="Hello there.", id="m2"),
                HumanMessage(content="what did I just say?", id="m3"),
            ],
        )
        assert second.succeeded
        assert "Hello there." in llm.last_prompt_text

        snapshot = await graph.aget_state(_config(conversation_id))  # type: ignore[arg-type]
        contents = [message.content for message in snapshot.values["messages"]]
        # Four turns, not six: the supplied history replaced the checkpoint's
        # copy instead of being appended to it.
        assert contents == ["hello", "Hello there.", "what did I just say?", "You said hello."]


async def test_a_retry_after_a_failure_leaves_no_orphaned_turn(settings: Settings) -> None:
    """The failed attempt's question must not appear twice once it succeeds."""
    conversation_id = uuid.uuid4()
    search = FakeSearchProvider()
    question = HumanMessage(content="hello", id="m1")

    async with checkpointer_for(settings) as saver:
        graph = build_graph(saver)
        failed = await run_agent(
            graph,
            context=context_for(
                conversation_id, FakeLLM(answer_error=LLMTransientError("503")), search
            ),
            history=[question],
        )
        assert not failed.succeeded

        retried = await run_agent(
            graph,
            context=context_for(conversation_id, FakeLLM(answers=["Hi."]), search),
            history=[question],
        )
        assert retried.succeeded

        snapshot = await graph.aget_state(_config(conversation_id))  # type: ignore[arg-type]
        contents = [message.content for message in snapshot.values["messages"]]
        assert contents == ["hello", "Hi."]


async def test_threads_are_isolated_from_each_other(settings: Settings) -> None:
    """A conversation id is the thread id, so two conversations never mix."""
    search = FakeSearchProvider()
    async with checkpointer_for(settings) as saver:
        graph = build_graph(saver)

        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        await run_agent(
            graph,
            context=context_for(first_id, FakeLLM(answers=["One."]), search),
            history=[HumanMessage(content="first conversation")],
        )
        llm = FakeLLM(answers=["Two."])
        await run_agent(
            graph,
            context=context_for(second_id, llm, search),
            history=[HumanMessage(content="second conversation")],
        )

        assert "first conversation" not in llm.last_prompt_text

        snapshot = await graph.aget_state(_config(second_id))  # type: ignore[arg-type]
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
            history=[HumanMessage(content="what is the latest news")],
        )

        snapshot = await graph.aget_state(_config(conversation_id))  # type: ignore[arg-type]
        values = snapshot.values
        assert "llm" not in values
        assert "search" not in values
        assert SECRET_KEY not in repr(values)
        # Sources survive the round trip as plain data.
        assert values["sources"][0]["source_id"] == "S1"
        assert values["sources"][0]["retrieved_at"].tzinfo is not None
