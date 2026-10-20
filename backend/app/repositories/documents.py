"""Document, chunk, and embedding-usage queries.

Every read a request can trigger takes ``user_id`` and ``conversation_id`` and
puts both in the WHERE clause, the same rule as the other repositories: there
is no fetch-by-id that a service could call and forget to check. The
ingestion methods at the bottom are the exception. They run in the background
on behalf of no request, and address a document only by an id that the
claim query itself returned.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    CursorResult,
    Table,
    and_,
    bindparam,
    delete,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import EMBEDDING_VECTOR_DIMENSIONS
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.models.embedding_usage import EmbeddingPurpose, EmbeddingUsage

# Rough tokens per character of English text, for estimating what an upload
# will cost before it is embedded. The ledger records the real count.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class ClaimedDocument:
    id: uuid.UUID
    user_id: uuid.UUID
    attempts: int


@dataclass(frozen=True, slots=True)
class PendingChunk:
    id: uuid.UUID
    content: str


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- request side ------------------------------------------------------

    def add(self, document: Document, chunks: Sequence[DocumentChunk]) -> None:
        self._session.add(document)
        self._session.add_all(chunks)

    async def get(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, document_id: uuid.UUID
    ) -> Document | None:
        result = await self._session.execute(
            select(Document).where(
                Document.id == document_id,
                Document.user_id == user_id,
                Document.conversation_id == conversation_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_conversation(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Sequence[Document]:
        """Upload order. Not paginated: a conversation holds a handful at most."""
        result = await self._session.execute(
            select(Document)
            .where(Document.user_id == user_id, Document.conversation_id == conversation_id)
            .order_by(Document.created_at, Document.id)
        )
        return result.scalars().all()

    async def find_by_sha(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, content_sha256: str
    ) -> Document | None:
        result = await self._session.execute(
            select(Document).where(
                Document.user_id == user_id,
                Document.conversation_id == conversation_id,
                Document.content_sha256 == content_sha256,
            )
        )
        return result.scalar_one_or_none()

    async def count_for_conversation(self, *, conversation_id: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.conversation_id == conversation_id)
        )
        return int(count or 0)

    async def delete(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, document_id: uuid.UUID
    ) -> bool:
        """Hard delete; chunks go with it by cascade. False if it was not the user's."""
        result = await self._session.execute(
            delete(Document).where(
                Document.id == document_id,
                Document.user_id == user_id,
                Document.conversation_id == conversation_id,
            )
        )
        return cast("CursorResult[Any]", result).rowcount > 0

    async def delete_for_conversation(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> int:
        result = await self._session.execute(
            delete(Document).where(
                Document.user_id == user_id, Document.conversation_id == conversation_id
            )
        )
        return cast("CursorResult[Any]", result).rowcount

    # --- quota -------------------------------------------------------------

    async def tokens_spent_since(
        self, *, user_id: uuid.UUID, since: datetime
    ) -> tuple[int, datetime | None]:
        """Tokens recorded in the window, and when the oldest of them was spent."""
        row = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(EmbeddingUsage.tokens), 0),
                    func.min(EmbeddingUsage.created_at),
                ).where(EmbeddingUsage.user_id == user_id, EmbeddingUsage.created_at >= since)
            )
        ).one()
        return int(row[0]), row[1]

    async def tokens_pending(self, *, user_id: uuid.UUID) -> int:
        """Estimated tokens still to be spent on the user's unfinished documents.

        Counted against the quota so that five uploads in quick succession
        cannot all pass the check before the first one has spent anything.
        """
        # ``//``: SQLAlchemy 2 compiles ``/`` on integers to true division.
        estimate = func.greatest(
            Document.char_count // CHARS_PER_TOKEN - Document.embedding_tokens, 0
        )
        total = await self._session.scalar(
            select(func.coalesce(func.sum(estimate), 0)).where(
                Document.user_id == user_id, Document.status == DocumentStatus.processing
            )
        )
        return int(total or 0)

    async def record_usage(
        self, *, user_id: uuid.UUID, purpose: EmbeddingPurpose, tokens: int
    ) -> None:
        if tokens > 0:
            self._session.add(EmbeddingUsage(user_id=user_id, purpose=purpose, tokens=tokens))

    # --- ingestion side ----------------------------------------------------

    async def claim_next(self, *, now: datetime, lease: timedelta) -> ClaimedDocument | None:
        """Take the oldest unfinished document nobody holds, for one lease.

        ``FOR UPDATE SKIP LOCKED`` lets every replica run this at once: each
        gets a different document, or none, and never waits on another. The
        attempt counter rises on every claim, so a document that crashes its
        worker every time still runs out of attempts.
        """
        candidate = (
            select(Document.id)
            .where(
                Document.status == DocumentStatus.processing,
                or_(Document.lease_expires_at.is_(None), Document.lease_expires_at < now),
            )
            .order_by(Document.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        result = await self._session.execute(
            update(Document)
            .where(Document.id == candidate)
            .values(
                lease_expires_at=now + lease,
                embedding_attempts=Document.embedding_attempts + 1,
            )
            .returning(Document.id, Document.user_id, Document.embedding_attempts)
        )
        row = result.one_or_none()
        return ClaimedDocument(id=row[0], user_id=row[1], attempts=row[2]) if row else None

    async def pending_chunks(self, *, document_id: uuid.UUID, limit: int) -> list[PendingChunk]:
        result = await self._session.execute(
            select(DocumentChunk.id, DocumentChunk.content)
            .where(DocumentChunk.document_id == document_id, DocumentChunk.embedding.is_(None))
            .order_by(DocumentChunk.ordinal)
            .limit(limit)
        )
        return [PendingChunk(id=row[0], content=row[1]) for row in result]

    async def store_embeddings(self, *, vectors: Sequence[tuple[uuid.UUID, list[float]]]) -> None:
        """Write one batch. A chunk deleted meanwhile is simply not updated.

        Core ``executemany`` rather than ORM bulk update: the ORM checks that
        every row it was told about still exists, and a document deleted while
        it was being embedded is a normal event here, not an error.
        """
        # Declarative models are typed with a generic FromClause; it is a Table.
        table = cast(Table, DocumentChunk.__table__)
        statement = (
            update(table)
            .where(table.c.id == bindparam("chunk_id"))
            .values(embedding=bindparam("vector", type_=VECTOR(EMBEDDING_VECTOR_DIMENSIONS)))
        )
        await self._session.execute(
            statement, [{"chunk_id": chunk_id, "vector": vector} for chunk_id, vector in vectors]
        )

    async def lock_for_batch(
        self, *, document_id: uuid.UUID, tokens: int, lease_until: datetime
    ) -> bool:
        """Count the batch's tokens, renew the lease, and lock the document row.

        The first write of every batch transaction, so the batch locks the
        document *before* its chunks, the same order a delete takes them in
        (the row, then its chunks by cascade). The other order deadlocks
        against a concurrent delete. False when the document is gone.
        """
        result = await self._session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(
                embedding_tokens=Document.embedding_tokens + tokens,
                lease_expires_at=lease_until,
            )
        )
        return cast("CursorResult[Any]", result).rowcount > 0

    async def mark_ready(self, *, document_id: uuid.UUID) -> bool:
        """Ready only if every chunk has its vector; checked in the same statement."""
        unembedded = exists().where(
            and_(DocumentChunk.document_id == document_id, DocumentChunk.embedding.is_(None))
        )
        result = await self._session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.status == DocumentStatus.processing,
                ~unembedded,
            )
            .values(status=DocumentStatus.ready, lease_expires_at=None, error_code=None)
        )
        return cast("CursorResult[Any]", result).rowcount > 0

    async def mark_failed(self, *, document_id: uuid.UUID, error_code: str) -> None:
        await self._session.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == DocumentStatus.processing)
            .values(status=DocumentStatus.failed, error_code=error_code, lease_expires_at=None)
        )

    async def back_off(self, *, document_id: uuid.UUID, until: datetime) -> None:
        """Leave the document for the sweep to retry no earlier than ``until``."""
        await self._session.execute(
            update(Document).where(Document.id == document_id).values(lease_expires_at=until)
        )
