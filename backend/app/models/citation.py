"""A source an assistant message cites: a web page or a chunk of a document."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CitationKind(StrEnum):
    web = "web"
    document = "document"


citation_kind_enum = Enum(CitationKind, name="citation_kind", create_type=False)


class Citation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "citations"

    message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    # 1-based, in the order the answer first cites each source. Stable: it is
    # what the rendered source list is numbered by.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # The label the answer text uses (``S3``). Kept so inline markers in the
    # stored content can be resolved without re-running anything.
    source_id: Mapped[str] = mapped_column(String(16), nullable=False)

    kind: Mapped[CitationKind] = mapped_column(
        citation_kind_enum, default=CitationKind.web, server_default="web", nullable=False
    )

    # Web citations. Canonical (tracking parameters stripped, http/https
    # only), validated before it was ever written here.
    url: Mapped[str | None] = mapped_column(Text, default=None)
    display_url: Mapped[str | None] = mapped_column(String(100), default=None)

    # Document citations (ADR-0015). Both cascade: removing a document removes
    # every citation of it, and the ``[D1]`` marker left in the answer renders
    # as plain text. ``title`` holds the filename.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), default=None
    )
    document_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="CASCADE"), default=None
    )
    page: Mapped[int | None] = mapped_column(Integer, default=None)
    # Text, not a bounded string: titles and snippets come from the open web
    # and a length surprise must not fail the transaction that saves an answer.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str | None] = mapped_column(Text, default=None)
    snippet: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # Also the index that loads a message's citations in order.
        UniqueConstraint("message_id", "ordinal", name="uq_citations_message_ordinal"),
        CheckConstraint("ordinal > 0", name="ck_citations_ordinal_positive"),
        # Each kind carries what it needs to be shown and followed.
        CheckConstraint(
            "(kind = 'web' AND url IS NOT NULL AND display_url IS NOT NULL) OR "
            "(kind = 'document' AND document_id IS NOT NULL "
            "AND document_chunk_id IS NOT NULL AND page IS NOT NULL)",
            name="ck_citations_kind_fields",
        ),
        # PostgreSQL does not index foreign keys itself. Without these, every
        # deleted chunk would scan the whole citations table for its cascade.
        Index("ix_citations_document_chunk", "document_chunk_id"),
        Index("ix_citations_document", "document_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Citation message={self.message_id} ordinal={self.ordinal}>"
