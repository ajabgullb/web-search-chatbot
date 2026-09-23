"""Public message representation.

Only user and assistant turns appear here. System prompts and tool payloads
exist in the same table but are never serialized: they would leak prompt text
and raw provider output to the client.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.citation import Citation
from app.models.message import Message, MessageStatus


class PublicMessageRole(StrEnum):
    user = "user"
    assistant = "assistant"


class CitationRead(BaseModel):
    """A source an answer cites. ``source_id`` matches the ``[S1]`` markers in
    the message content; ``ordinal`` numbers the rendered source list."""

    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    source_id: str
    url: str
    display_url: str
    title: str
    publisher: str | None
    snippet: str
    published_at: datetime | None
    retrieved_at: datetime


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: PublicMessageRole
    content: str
    status: MessageStatus
    sequence_number: int
    created_at: datetime
    # True when the answer could not be verified against sources; ``notice``
    # says why in one sentence the UI can show as-is.
    degraded: bool = False
    notice: str | None = None
    citations: list[CitationRead] = Field(default_factory=list)

    @classmethod
    def build(cls, message: Message, citations: Sequence[Citation] = ()) -> MessageRead:
        """Serialize a message with its citations.

        Citations are passed in rather than read through an ORM relationship:
        with async SQLAlchemy an unloaded relationship raises on access, and an
        explicit argument makes every caller decide how they were loaded.
        """
        read = cls.model_validate(message)
        read.citations = [CitationRead.model_validate(citation) for citation in citations]
        return read
