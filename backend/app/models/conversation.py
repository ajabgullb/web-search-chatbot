"""A user's chat thread."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # Deleting an account takes its conversations with it; there is no
        # ownerless conversation the API could ever serve.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # Soft delete: the row stays so a later restore or audit is possible, and
    # so message rows keep a valid parent. Every read filters on it being NULL.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # Serves the sidebar listing directly: the keyset page reads
        # (user_id, updated_at DESC, id DESC) and skips deleted rows, so the
        # partial index means no filtering happens after the index scan.
        Index(
            "ix_conversations_user_recent",
            "user_id",
            text("updated_at DESC"),
            text("id DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Conversation id={self.id}>"
