"""One execution of the agent against one user message."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


ACTIVE_RUN_STATUSES = (RunStatus.queued, RunStatus.running)

run_status_enum = Enum(RunStatus, name="run_status", create_type=False)


class Run(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "runs"

    # Denormalized from the conversation so that ownership and the idempotency
    # scope are both a single-table predicate.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), default=None
    )

    status: Mapped[RunStatus] = mapped_column(
        run_status_enum, default=RunStatus.queued, nullable=False
    )
    # A stable machine code, never free text: messages shown to users are
    # derived from it, so no exception string can reach a client through here.
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_retryable: Mapped[bool | None] = mapped_column(Boolean, default=None)

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # SHA-256 of the request that first used the key. A second request with
    # the same key but a different body is a client bug, not a retry.
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    provider: Mapped[str | None] = mapped_column(String(50), default=None)
    model: Mapped[str | None] = mapped_column(String(100), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(50), default=None)
    search_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_runs_user_idempotency_key"),
        # The single-active-run rule, enforced by the database rather than by a
        # check-then-insert in Python: two concurrent submissions cannot both
        # commit, whatever the timing. The loser gets a unique violation.
        Index(
            "ux_runs_one_active_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_runs_conversation_created", "conversation_id", text("created_at DESC")),
        Index("ix_runs_user_message", "user_message_id"),
    )

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_RUN_STATUSES

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Run id={self.id} status={self.status}>"
