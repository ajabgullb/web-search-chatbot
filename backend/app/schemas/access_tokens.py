"""Personal access token representations (ADR-0019)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.personal_access_token import TokenScope

NAME_MAX_LENGTH = 100


class AccessTokenCreate(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=NAME_MAX_LENGTH,
        description="A label for the client the token is for, such as 'Claude Desktop'.",
    )
    scopes: list[TokenScope] = Field(min_length=1, max_length=len(TokenScope))
    expires_in_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Days until the token stops working. At most ACCESS_TOKEN_MAX_TTL_DAYS.",
    )


class AccessTokenRead(BaseModel):
    """A token as listed. The token itself is never shown again after creation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    token_prefix: str = Field(description="The token's first characters, to tell tokens apart.")
    scopes: list[TokenScope]
    expires_at: datetime
    last_used_at: datetime | None
    created_at: datetime


class AccessTokenCreated(AccessTokenRead):
    token: str = Field(
        description="The token. Shown in this response only: the server keeps just its hash."
    )


class AccessTokenList(BaseModel):
    items: list[AccessTokenRead]
