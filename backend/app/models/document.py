"""A PDF uploaded to one conversation, and the chunks it was split into.

See ADR-0015. Two properties shape both tables:

- **Scope.** A document belongs to exactly one conversation. ``user_id`` and
  ``conversation_id`` are both on every chunk, so the ownership filter and the
  conversation filter are single-table predicates in the retrieval query.
- **No original file.** The PDF is discarded after parsing. Chunk text is what
  remains, which is enough to re-embed with a new model but not to re-chunk.

Documents are hard-deleted, never soft-deleted: a user who removes a private
document must not leave its text behind.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import EMBEDDING_VECTOR_DIMENSIONS
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentStatus(StrEnum):
    # Parsed and chunked; embeddings are being written in the background.
    processing = "processing"
    ready = "ready"
    failed = "failed"


document_status_enum = Enum(DocumentStatus, name="document_status", create_type=False)

# The full-text configuration for the keyword half of hybrid search. "simple"
# lowercases and splits but does not stem or drop stop words, and assumes no
# language. That suits its job: catching the exact names, codes, and numbers
# that embeddings blur. Meaning is the embedding's job, in any language.
FULL_TEXT_CONFIG = "simple"


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )

    # Shown to the user and to the model as the source title. Sanitized before
    # it is written; never used as a path, because no file is ever stored.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Hex SHA-256 of the uploaded bytes. Recognises the same file uploaded to
    # the same conversation again, so it is not parsed or paid for twice.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[DocumentStatus] = mapped_column(
        document_status_enum, default=DocumentStatus.processing, nullable=False
    )
    # A stable machine code (``pdf_no_text``, ``embedding_unavailable``), never
    # free text, so no exception string can reach a client through here.
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)

    # What the chunks were embedded with. Retrieval compares only vectors from
    # the configured model: vectors from two models live in unrelated spaces.
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    # Provider-reported tokens spent so far, counted toward the user's quota.
    embedding_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    embedding_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # While set and in the future, one process owns the embedding work. A
    # process that dies holding it simply lets it expire, and the sweep
    # hands the document to another.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        UniqueConstraint("conversation_id", "content_sha256", name="uq_documents_conversation_sha"),
        # Lists a conversation's documents in upload order.
        Index("ix_documents_conversation_created", "conversation_id", "created_at"),
        # The sweep looks only at unfinished documents; there are few of them.
        Index(
            "ix_documents_processing_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'processing'"),
        ),
        CheckConstraint(
            "byte_size > 0 AND page_count > 0 AND char_count > 0 AND chunk_count > 0",
            name="ck_documents_counts_positive",
        ),
        CheckConstraint("embedding_tokens >= 0", name="ck_documents_tokens_nonnegative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Document id={self.id} status={self.status}>"


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized from the document so retrieval filters one table. Foreign
    # keys rather than plain columns, so they cannot drift from the truth.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )

    # Position within the document, from 0. Stable: citations point at it.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # 1-based. A chunk never spans pages, so a citation names exactly one.
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Maintained by the database from ``content``, so it can never be stale.
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('{FULL_TEXT_CONFIG}', content)", persisted=True),
        nullable=False,
    )
    # Null until the background embedder reaches this chunk. Retrieval reads
    # only chunks of ready documents, which never have a null embedding.
    embedding: Mapped[list[float] | None] = mapped_column(
        VECTOR(EMBEDDING_VECTOR_DIMENSIONS), default=None
    )

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_ordinal"),
        # Retrieval narrows to one conversation, then computes exact cosine
        # distance over at most a few thousand rows. That is why there is no
        # HNSW index: see ADR-0015 ("exact search, no ANN index").
        Index("ix_document_chunks_conversation", "conversation_id"),
        Index("ix_document_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
        # The embedder's work queue: chunks of one document still to embed.
        Index(
            "ix_document_chunks_unembedded",
            "document_id",
            postgresql_where=text("embedding IS NULL"),
        ),
        CheckConstraint("ordinal >= 0", name="ck_document_chunks_ordinal_nonnegative"),
        CheckConstraint("page > 0", name="ck_document_chunks_page_positive"),
        CheckConstraint("length(content) > 0", name="ck_document_chunks_content_nonempty"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DocumentChunk document={self.document_id} ordinal={self.ordinal}>"
