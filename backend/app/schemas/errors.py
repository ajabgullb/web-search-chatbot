"""The error envelope, declared as a model so it appears in OpenAPI."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable summary. May be reworded; do not parse.")
    request_id: str | None = Field(
        default=None, description="Correlates with the X-Request-ID response header."
    )
    details: dict[str, Any] | None = Field(
        default=None, description="Optional structured context, e.g. failing fields."
    )


class ErrorResponse(BaseModel):
    error: ErrorBody
