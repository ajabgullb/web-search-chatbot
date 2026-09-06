"""Refresh-session queries, including reuse detection and family revocation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_session import AuthSession


class AuthSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_token_hash(self, token_hash: str) -> AuthSession | None:
        result = await self._session.execute(
            select(AuthSession).where(AuthSession.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    def add(self, auth_session: AuthSession) -> AuthSession:
        self._session.add(auth_session)
        return auth_session

    async def revoke_family(self, family_id: uuid.UUID, *, at: datetime) -> int:
        """Revoke every live token descended from one login.

        Called when a already-rotated token is presented: either the token was
        stolen or the legitimate client replayed it, and neither case is safe
        to keep serving. Returns the number of sessions revoked.
        """
        result = await self._session.execute(
            update(AuthSession)
            .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        return cast("CursorResult[Any]", result).rowcount or 0

    async def revoke_all_for_user(self, user_id: uuid.UUID, *, at: datetime) -> int:
        result = await self._session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        return cast("CursorResult[Any]", result).rowcount or 0

    async def revoke(self, auth_session: AuthSession, *, at: datetime) -> None:
        auth_session.revoked_at = at

    async def count_active_for_user(self, user_id: uuid.UUID, *, now: datetime) -> int:
        result = await self._session.execute(
            select(AuthSession.id).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )
        return len(result.scalars().all())
