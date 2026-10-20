"""Embedding tokens spent, per user: the ledger behind the daily quota.

Append-only, and deliberately not linked to ``documents``. If usage were
counted on document rows, deleting a document would refund its tokens, and a
user could reset the quota by deleting and re-uploading. A row here outlives
whatever it was spent on; only deleting the account removes it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class EmbeddingPurpose(StrEnum):
    document = "document"
    query = "query"


embedding_purpose_enum = Enum(EmbeddingPurpose, name="embedding_purpose", create_type=False)


class EmbeddingUsage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "embedding_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[EmbeddingPurpose] = mapped_column(embedding_purpose_enum, nullable=False)
    # As reported by the provider, so the quota counts what was billed.
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The quota sums one user's rows over the last 24 hours on every upload.
        Index("ix_embedding_usage_user_created", "user_id", "created_at"),
        CheckConstraint("tokens > 0", name="ck_embedding_usage_tokens_positive"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EmbeddingUsage user={self.user_id} tokens={self.tokens}>"
