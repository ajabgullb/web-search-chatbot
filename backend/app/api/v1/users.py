"""Account endpoints for the signed-in user."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import CurrentUserDep
from app.schemas.user import UserRead

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead)
async def read_current_user(current_user: CurrentUserDep) -> UserRead:
    return UserRead.model_validate(current_user)
