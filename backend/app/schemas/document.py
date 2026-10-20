"""Public document representations (ADR-0015).

Metadata only. Chunk text is never returned by the API: it reaches the model
during a run and the user as a cited excerpt, not as a download.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import DocumentStatus


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: DocumentStatus = Field(
        description=(
            "processing: stored and being prepared; ready: answers can use it; "
            "failed: it could not be prepared (see error_code). Poll while processing."
        )
    )
    error_code: str | None = Field(
        default=None, description="Set when status is failed, e.g. embedding_unavailable."
    )
    byte_size: int
    page_count: int
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class DocumentList(BaseModel):
    """Every document in the conversation, in upload order. Capped, so unpaginated."""

    items: list[DocumentRead]
