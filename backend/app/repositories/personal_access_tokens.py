"""Personal access token queries (ADR-0019).

Every query that takes a token id also takes the owner's id, so a guessed id
finds nothing, the same rule conversations follow.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.personal_access_token import PersonalAccessToken

# How stale ``last_used_at`` may get. It answers "is this token still in use?"
# in the token list, where a minute's precision is plenty, and it saves a
# write on almost every request a busy client makes.
LAST_USED_RESOLUTION = timedelta(minutes=1)


class PersonalAccessTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, token: PersonalAccessToken) -> PersonalAccessToken:
        self._session.add(token)
        return token

    async def get_by_token_hash(self, token_hash: str) -> PersonalAccessToken | None:
        result = await self._session.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def list_for_user(self, *, user_id: uuid.UUID) -> Sequence[PersonalAccessToken]:
        """Every token the user has not revoked, newest first, expired ones included.

        An expired token stays listed so the user can see why a client stopped
        working. Revoked ones are gone: the user removed them on purpose.
        """
        result = await self._session.execute(
            select(PersonalAccessToken)
            .where(
                PersonalAccessToken.user_id == user_id,
                PersonalAccessToken.revoked_at.is_(None),
            )
            .order_by(PersonalAccessToken.created_at.desc(), PersonalAccessToken.id.desc())
        )
        return result.scalars().all()

    async def count_live_for_user(self, *, user_id: uuid.UUID, now: datetime) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(PersonalAccessToken)
            .where(
                PersonalAccessToken.user_id == user_id,
                PersonalAccessToken.revoked_at.is_(None),
                PersonalAccessToken.expires_at > now,
            )
        )
        return int(result.scalar_one())

    async def revoke(self, *, user_id: uuid.UUID, token_id: uuid.UUID, at: datetime) -> bool:
        """Revoke one of the user's tokens. False if no such live token is theirs."""
        result = await self._session.execute(
            update(PersonalAccessToken)
            .where(
                PersonalAccessToken.id == token_id,
                PersonalAccessToken.user_id == user_id,
                PersonalAccessToken.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        return (cast("CursorResult[Any]", result).rowcount or 0) > 0

    async def revoke_all_for_user(self, *, user_id: uuid.UUID, at: datetime) -> int:
        result = await self._session.execute(
            update(PersonalAccessToken)
            .where(
                PersonalAccessToken.user_id == user_id,
                PersonalAccessToken.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        return cast("CursorResult[Any]", result).rowcount or 0

    async def touch(self, *, token_id: uuid.UUID, at: datetime) -> None:
        """Record a use, unless one was recorded within the last minute.

        The condition is in the UPDATE itself, so concurrent requests cannot
        both decide to write.
        """
        await self._session.execute(
            update(PersonalAccessToken)
            .where(
                PersonalAccessToken.id == token_id,
                or_(
                    PersonalAccessToken.last_used_at.is_(None),
                    PersonalAccessToken.last_used_at < at - LAST_USED_RESOLUTION,
                ),
            )
            .values(last_used_at=at)
        )
