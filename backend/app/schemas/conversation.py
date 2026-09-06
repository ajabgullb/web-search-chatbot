"""Public conversation representations."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

TITLE_MAX_LENGTH = 200


class ConversationCreate(BaseModel):
    """A new thread. The title is optional — most clients let it be derived."""

    title: str | None = Field(default=None, max_length=TITLE_MAX_LENGTH)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    # Bumped by any write to the thread, and the primary sort key of the
    # sidebar listing, so clients can order by it without a second field.
    updated_at: datetime
