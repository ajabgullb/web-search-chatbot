"""Response models for the health endpoints.

Routes return Pydantic models, never ORM objects or bare dicts, so the public
shape always appears in the generated OpenAPI schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LivenessResponse(BaseModel):
    """Process is up. Deliberately independent of every external dependency."""

    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    """Process can serve traffic: required dependencies answered."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, bool] = Field(
        description="Per-dependency result, keyed by dependency name.",
    )
