"""Password-reset token queries."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.password_reset_token import PasswordResetToken


class PasswordResetTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_token_hash(self, token_hash: str) -> PasswordResetToken | None:
        result = await self._session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    def add(self, token: PasswordResetToken) -> PasswordResetToken:
        self._session.add(token)
        return token

    async def invalidate_outstanding_for_user(self, user_id: uuid.UUID, *, at: datetime) -> int:
        """Burn any unused tokens before issuing a new one.

        Without this, every "forgot password" click would leave another valid
        credential in an inbox.
        """
        result = await self._session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=at)
        )
        return cast("CursorResult[Any]", result).rowcount or 0
