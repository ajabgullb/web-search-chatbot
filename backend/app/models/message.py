"""A single turn in a conversation."""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MessageRole(StrEnum):
    """Who produced the message.

    ``system`` and ``tool`` exist for the agent's internal transcript; the
    public API serializes user and assistant turns only.
    """

    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class MessageStatus(StrEnum):
    complete = "complete"
    pending = "pending"
    failed = "failed"
    cancelled = "cancelled"


# Named types are created once by the migration; the models must not try to
# emit CREATE TYPE again when a table is created in a test.
message_role_enum = Enum(MessageRole, name="message_role", create_type=False)
message_status_enum = Enum(MessageStatus, name="message_status", create_type=False)


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(message_role_enum, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[MessageStatus] = mapped_column(
        message_status_enum,
        default=MessageStatus.complete,
        nullable=False,
    )

    # Ordering within the thread. Assigned by the application rather than taken
    # from a timestamp, because two rows written in the same millisecond must
    # still have a total order; the unique constraint below enforces it.
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Provenance, recorded when a model produced the turn. Null for user input.
    model: Mapped[str | None] = mapped_column(String(100), default=None)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    __table_args__ = (
        # The real guarantee behind message ordering. A concurrent writer that
        # races for the same slot loses here rather than producing a thread
        # whose order depends on read timing.
        UniqueConstraint("conversation_id", "sequence_number", name="uq_messages_conversation_seq"),
        CheckConstraint("sequence_number > 0", name="ck_messages_sequence_positive"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Message id={self.id} role={self.role}>"
