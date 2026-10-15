"""Public user representation."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class UserRead(BaseModel):
    """What the API exposes about an account. No password hash, ever."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    is_active: bool
    # The client shows a "confirm your email" prompt while this is false;
    # the server enforces it independently when a run is started.
    is_verified: bool
    created_at: datetime
