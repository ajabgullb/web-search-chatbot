"""A source an assistant message cites."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


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

    # Canonical (tracking parameters stripped, http/https only) — validated
    # before it was ever written here.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    display_url: Mapped[str] = mapped_column(String(100), nullable=False)
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
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Citation message={self.message_id} ordinal={self.ordinal}>"
