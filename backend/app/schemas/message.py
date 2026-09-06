"""Public message representation.

Only user and assistant turns appear here. System prompts and tool payloads
exist in the same table but are never serialized: they would leak prompt text
and raw provider output to the client.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.models.message import MessageStatus


class PublicMessageRole(StrEnum):
    user = "user"
    assistant = "assistant"


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: PublicMessageRole
    content: str
    status: MessageStatus
    sequence_number: int
    created_at: datetime
