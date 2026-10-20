"""Document and chunk storage against a real pgvector-enabled PostgreSQL.

These pin down what later slices rely on from the database itself: a vector
survives the asyncpg round-trip, cosine distance and the full-text column both
work in SQL, malformed rows are refused, and deletes leave no text behind.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.config import EMBEDDING_VECTOR_DIMENSIONS, Settings
from app.db.session import Database
from app.models import Conversation, Document, DocumentChunk, DocumentStatus, User
from tests.fakes import FakeEmbeddings

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings)
    try:
        yield db
    finally:
        await db.dispose()


async def make_owner(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    """A user and one of their conversations; returns their IDs."""
    async with database.open_session() as session:
        user = User(
            email=f"{uuid.uuid4().hex}@example.com",
            password_hash="not-a-real-hash",
            display_name="Reader",
        )
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id, title="Papers")
        session.add(conversation)
        await session.commit()
        return user.id, conversation.id


def new_document(
    user_id: uuid.UUID, conversation_id: uuid.UUID, *, content: bytes = b"%PDF-1"
) -> Document:
    return Document(
        user_id=user_id,
        conversation_id=conversation_id,
        filename="report.pdf",
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        page_count=1,
        char_count=100,
        chunk_count=1,
        status=DocumentStatus.processing,
        embedding_model="fake-embedding",
        embedding_dimensions=EMBEDDING_VECTOR_DIMENSIONS,
    )


async def store(
    database: Database, user_id: uuid.UUID, conversation_id: uuid.UUID, texts: list[str]
) -> uuid.UUID:
    """One document whose chunks are ``texts``, embedded with the fake."""
    vectors = (await FakeEmbeddings().embed(texts)).vectors
    async with database.open_session() as session:
        document = new_document(user_id, conversation_id, content="".join(texts).encode())
        document.chunk_count = len(texts)
        session.add(document)
        await session.flush()
        for ordinal, (content, vector) in enumerate(zip(texts, vectors, strict=True)):
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    ordinal=ordinal,
                    page=1,
                    content=content,
                    embedding=vector,
                )
            )
        await session.commit()
        return document.id


async def test_a_vector_round_trips_through_asyncpg(database: Database) -> None:
    """pgvector's SQLAlchemy type sends vectors as text, so asyncpg needs no
    custom codec registered on each connection."""
    user_id, conversation_id = await make_owner(database)
    await store(database, user_id, conversation_id, ["revenue grew twelve percent"])
    expected = FakeEmbeddings().vector("revenue grew twelve percent")

    async with database.open_session() as session:
        stored = await session.scalar(select(DocumentChunk.embedding))

    assert stored is not None
    assert len(stored) == EMBEDDING_VECTOR_DIMENSIONS
    # float4 storage: equal to single precision, not to the last bit.
    assert max(abs(a - b) for a, b in zip(stored, expected, strict=True)) < 1e-6


async def test_cosine_distance_ranks_the_related_chunk_first(database: Database) -> None:
    user_id, conversation_id = await make_owner(database)
    await store(
        database,
        user_id,
        conversation_id,
        ["the cat sat on the mat", "revenue growth was twelve percent", "a recipe for bread"],
    )
    query = FakeEmbeddings().vector("what was the revenue growth")

    async with database.open_session() as session:
        ranked = (
            await session.scalars(
                select(DocumentChunk.content)
                .where(DocumentChunk.conversation_id == conversation_id)
                .order_by(DocumentChunk.embedding.cosine_distance(query))
            )
        ).all()

    assert ranked[0] == "revenue growth was twelve percent"


async def test_the_full_text_column_is_maintained_by_the_database(database: Database) -> None:
    """Exact terms such as product codes are what the keyword half is for."""
    user_id, conversation_id = await make_owner(database)
    await store(
        database,
        user_id,
        conversation_id,
        ["Part number XR-7731 failed inspection", "Everything else passed"],
    )

    async with database.open_session() as session:
        matched = (
            await session.scalars(
                select(DocumentChunk.content).where(
                    DocumentChunk.content_tsv.op("@@")(
                        func.websearch_to_tsquery("simple", "XR-7731")
                    )
                )
            )
        ).all()

    assert matched == ["Part number XR-7731 failed inspection"]


async def test_a_vector_of_the_wrong_width_is_refused(database: Database) -> None:
    user_id, conversation_id = await make_owner(database)
    document_id = await store(database, user_id, conversation_id, ["first"])

    async with database.open_session() as session:
        session.add(
            DocumentChunk(
                document_id=document_id,
                user_id=user_id,
                conversation_id=conversation_id,
                ordinal=1,
                page=1,
                content="second",
                embedding=[0.1] * 512,
            )
        )
        with pytest.raises(DBAPIError, match="expected 1536 dimensions"):
            await session.commit()


async def test_an_empty_chunk_is_refused(database: Database) -> None:
    user_id, conversation_id = await make_owner(database)
    document_id = await store(database, user_id, conversation_id, ["first"])

    async with database.open_session() as session:
        session.add(
            DocumentChunk(
                document_id=document_id,
                user_id=user_id,
                conversation_id=conversation_id,
                ordinal=1,
                page=1,
                content="",
            )
        )
        with pytest.raises(IntegrityError, match="ck_document_chunks_content_nonempty"):
            await session.commit()


async def test_the_same_file_is_unique_per_conversation_only(database: Database) -> None:
    """A re-upload to the same chat is recognised; the same file in another chat
    is a separate document, because documents never cross conversations."""
    user_id, conversation_id = await make_owner(database)
    _, other_conversation_id = await make_owner(database)

    async with database.open_session() as session:
        session.add(new_document(user_id, conversation_id))
        session.add(new_document(user_id, other_conversation_id))
        await session.commit()

    async with database.open_session() as session:
        session.add(new_document(user_id, conversation_id))
        with pytest.raises(IntegrityError, match="uq_documents_conversation_sha"):
            await session.commit()


async def test_deleting_a_document_deletes_its_text(database: Database) -> None:
    user_id, conversation_id = await make_owner(database)
    document_id = await store(database, user_id, conversation_id, ["private", "text"])

    async with database.open_session() as session:
        await session.execute(delete(Document).where(Document.id == document_id))
        await session.commit()
        remaining = await session.scalar(select(func.count()).select_from(DocumentChunk))

    assert remaining == 0


async def test_deleting_an_account_deletes_its_documents(database: Database) -> None:
    user_id, conversation_id = await make_owner(database)
    await store(database, user_id, conversation_id, ["private"])

    async with database.open_session() as session:
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
        documents = await session.scalar(select(func.count()).select_from(Document))
        chunks = await session.scalar(select(func.count()).select_from(DocumentChunk))

    assert (documents, chunks) == (0, 0)


async def test_no_approximate_vector_index_exists(database: Database) -> None:
    """ADR-0015: exact search within one conversation. An HNSW index would
    filter after the neighbour search and could drop a user's own chunks."""
    async with database.open_session() as session:
        definitions = (
            await session.scalars(
                text("SELECT indexdef FROM pg_indexes WHERE tablename = 'document_chunks'")
            )
        ).all()

    assert not any("hnsw" in d or "ivfflat" in d for d in definitions)
