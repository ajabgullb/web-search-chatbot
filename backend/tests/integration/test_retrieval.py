"""Document retrieval against real PostgreSQL with pgvector (ADR-0015).

The security property first: a search sees one user's one conversation and
nothing else, enforced in SQL. Then the selection rules as the database
applies them, and an answer citing a document from upload to deletion.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from app.agent.context import AgentLimits
from app.core.config import EMBEDDING_VECTOR_DIMENSIONS
from app.db.session import Database
from app.models import (
    Conversation,
    Document,
    DocumentChunk,
    DocumentStatus,
    EmbeddingPurpose,
    EmbeddingUsage,
    User,
)
from app.providers.embeddings import EmbeddingAuthError
from app.services.retrieval import PostgresDocumentRetriever
from tests.fakes import FakeEmbeddings, FakeLLM
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import install_providers, parse_sse, post_run
from tests.pdfs import make_pdf

pytestmark = pytest.mark.integration

LIMITS = AgentLimits(
    max_searches_per_run=3,
    max_results_per_search=5,
    search_timeout_seconds=20.0,
    run_timeout_seconds=90.0,
    max_output_tokens=1_500,
    max_context_messages=20,
    # The fake embedder's similarities sit lower than a real model's.
    document_min_similarity=0.2,
)

REVENUE = "Quarterly revenue grew twelve percent on strong demand."
WEATHER = "The office picnic was cancelled because of heavy rain."
QUESTION = "how much did quarterly revenue grow"


@pytest.fixture
async def embeddings(app: FastAPI, client: AsyncClient) -> FakeEmbeddings:
    await app.state.embedding_provider.aclose()
    fake = FakeEmbeddings()
    app.state.embedding_provider = fake
    return fake


@pytest.fixture
def retriever(app: FastAPI, embeddings: FakeEmbeddings) -> PostgresDocumentRetriever:
    return PostgresDocumentRetriever(app.state.database, embeddings=lambda: embeddings)


async def owner(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    """A new user and one of their conversations."""
    async with database.open_session() as session:
        user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x", display_name="U")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id, title="Docs")
        session.add(conversation)
        await session.commit()
        return user.id, conversation.id


async def store(
    database: Database,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    texts: list[str],
    *,
    status: DocumentStatus = DocumentStatus.ready,
    model: str = "fake-embedding",
    filename: str = "report.pdf",
) -> uuid.UUID:
    """A document with one chunk per text, on pages 1, 2, …, already embedded."""
    fake = FakeEmbeddings()
    async with database.open_session() as session:
        document = Document(
            user_id=user_id,
            conversation_id=conversation_id,
            filename=filename,
            content_sha256=uuid.uuid4().hex,
            byte_size=100,
            page_count=len(texts),
            char_count=sum(map(len, texts)),
            chunk_count=len(texts),
            status=status,
            embedding_model=model,
            embedding_dimensions=EMBEDDING_VECTOR_DIMENSIONS,
        )
        session.add(document)
        await session.flush()
        for ordinal, text in enumerate(texts):
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    ordinal=ordinal,
                    page=ordinal + 1,
                    content=text,
                    embedding=fake.vector(text),
                )
            )
        await session.commit()
        return document.id


def contents(search: Any) -> list[str]:
    return [ranked.candidate.content for ranked in search.chunks]


class TestIsolation:
    async def test_another_users_identical_document_is_never_returned(
        self, app: FastAPI, retriever: PostgresDocumentRetriever
    ) -> None:
        database: Database = app.state.database
        alice, alices_chat = await owner(database)
        bob, bobs_chat = await owner(database)
        await store(database, alice, alices_chat, [REVENUE], filename="alice.pdf")
        await store(database, bob, bobs_chat, [REVENUE], filename="bob.pdf")

        found = await retriever.search(
            user_id=bob, conversation_id=bobs_chat, query=QUESTION, limits=LIMITS
        )

        assert [r.candidate.filename for r in found.chunks] == ["bob.pdf"]

    async def test_naming_someone_elses_conversation_finds_nothing(
        self, app: FastAPI, retriever: PostgresDocumentRetriever
    ) -> None:
        """Ownership is in the WHERE clause, not in the caller's good intentions."""
        database: Database = app.state.database
        alice, alices_chat = await owner(database)
        bob, _ = await owner(database)
        await store(database, alice, alices_chat, [REVENUE])

        found = await retriever.search(
            user_id=bob, conversation_id=alices_chat, query=QUESTION, limits=LIMITS
        )
        names = await retriever.ready_documents(user_id=bob, conversation_id=alices_chat)

        assert found.chunks == []
        assert names == []

    async def test_a_document_stays_in_its_own_conversation(
        self, app: FastAPI, retriever: PostgresDocumentRetriever
    ) -> None:
        database: Database = app.state.database
        user, first_chat = await owner(database)
        async with database.open_session() as session:
            second = Conversation(user_id=user, title="Other")
            session.add(second)
            await session.commit()
            second_chat = second.id
        await store(database, user, first_chat, [REVENUE])

        found = await retriever.search(
            user_id=user, conversation_id=second_chat, query=QUESTION, limits=LIMITS
        )

        assert found.chunks == []


class TestSelection:
    async def test_the_relevant_chunk_is_found_and_the_unrelated_one_is_not(
        self, app: FastAPI, retriever: PostgresDocumentRetriever
    ) -> None:
        database: Database = app.state.database
        user, chat = await owner(database)
        await store(database, user, chat, [WEATHER, REVENUE])

        found = await retriever.search(
            user_id=user, conversation_id=chat, query=QUESTION, limits=LIMITS
        )

        assert contents(found) == [REVENUE]
        assert found.chunks[0].candidate.page == 2

    async def test_an_exact_code_is_found_by_keyword_below_the_floor(
        self, app: FastAPI, retriever: PostgresDocumentRetriever
    ) -> None:
        database: Database = app.state.database
        user, chat = await owner(database)
        await store(database, user, chat, ["Inspection log: unit XR7731 overheated twice."])
        strict = replace(LIMITS, document_min_similarity=0.99)

        found = await retriever.search(
            user_id=user, conversation_id=chat, query="what happened to XR7731", limits=strict
        )

        assert len(found.chunks) == 1
        assert found.chunks[0].keyword_match is True

    @pytest.mark.parametrize(
        ("status", "model"),
        [
            (DocumentStatus.processing, "fake-embedding"),
            (DocumentStatus.failed, "fake-embedding"),
            (DocumentStatus.ready, "some-other-model"),
        ],
    )
    async def test_only_ready_documents_from_the_same_model_are_searched(
        self,
        app: FastAPI,
        retriever: PostgresDocumentRetriever,
        status: DocumentStatus,
        model: str,
    ) -> None:
        """Vectors from another model live in another space; comparing them
        would rank noise."""
        database: Database = app.state.database
        user, chat = await owner(database)
        await store(database, user, chat, [REVENUE], status=status, model=model)

        found = await retriever.search(
            user_id=user, conversation_id=chat, query=QUESTION, limits=LIMITS
        )

        assert found.chunks == []
        assert await retriever.ready_documents(user_id=user, conversation_id=chat) == []


class TestAccounting:
    async def test_the_question_is_charged_to_the_ledger_as_a_query(
        self, app: FastAPI, retriever: PostgresDocumentRetriever
    ) -> None:
        database: Database = app.state.database
        user, chat = await owner(database)
        await store(database, user, chat, [REVENUE])

        await retriever.search(user_id=user, conversation_id=chat, query=QUESTION, limits=LIMITS)

        async with database.open_session() as session:
            purposes = (await session.scalars(select(EmbeddingUsage.purpose))).all()
        assert purposes == [EmbeddingPurpose.query]

    async def test_an_embedding_failure_reports_failed_instead_of_raising(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        database: Database = app.state.database
        user, chat = await owner(database)
        failing = FakeEmbeddings(error=EmbeddingAuthError("bad key"))
        retriever = PostgresDocumentRetriever(database, embeddings=lambda: failing)

        found = await retriever.search(
            user_id=user, conversation_id=chat, query=QUESTION, limits=LIMITS
        )

        assert found.failed is True
        assert found.chunks == []


async def test_an_answer_cites_a_page_of_an_uploaded_pdf_until_it_is_removed(
    app: FastAPI, client: AsyncClient, embeddings: FakeEmbeddings
) -> None:
    """Upload, ask, see a page citation stream and persist; remove the
    document, and the citation goes with it while the answer text stays."""
    app.state.agent_limits = LIMITS
    headers = auth_header(await register(client, email="reader@example.com"))
    conversation_id = str((await create_conversation(client, headers))["id"])
    pdf = make_pdf(["Contents", "Quarterly revenue grew twelve percent on strong demand."])
    uploaded = await client.post(
        f"/api/v1/conversations/{conversation_id}/documents",
        files={"file": ("report.pdf", pdf, "application/pdf")},
        headers=headers,
    )
    document_id = uploaded.json()["id"]
    async with asyncio.timeout(15):
        while (
            await client.get(
                f"/api/v1/conversations/{conversation_id}/documents/{document_id}",
                headers=headers,
            )
        ).json()["status"] != "ready":
            await asyncio.sleep(0.05)

    await install_providers(app, llm=FakeLLM(answers=["Revenue grew twelve percent [D1]."]))
    stream = parse_sse(
        (
            await post_run(
                client,
                headers,
                conversation_id,
                content="What does the report say about quarterly revenue?",
            )
        ).text
    )

    types = [event["type"] for event in stream.events]
    assert "documents.retrieved" in types
    retrieved = next(e for e in stream.events if e["type"] == "documents.retrieved")
    assert [d["filename"] for d in retrieved["documents"]] == ["report.pdf"]
    completed = stream.terminal
    assert completed["type"] == "message.completed"
    [citation] = completed["message"]["citations"]
    assert (citation["kind"], citation["page"], citation["url"]) == ("document", 2, None)
    assert citation["document_id"] == document_id

    # Removing the document removes the citation; the text keeps its marker.
    removed = await client.delete(
        f"/api/v1/conversations/{conversation_id}/documents/{document_id}", headers=headers
    )
    assert removed.status_code == 204
    history = (
        await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    ).json()["items"]
    answer = history[-1]
    assert answer["citations"] == []
    assert "[D1]" in answer["content"]

    database: Database = app.state.database
    async with database.open_session() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
