"""User queries. The only module that writes SQL against ``users``."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, normalized_email: str) -> User | None:
        """Look up by the already-normalized address.

        Callers pass the output of ``normalize_email``; doing the normalizing
        here as well would hide a caller that forgot to.
        """
        result = await self._session.execute(select(User).where(User.email == normalized_email))
        return result.scalar_one_or_none()

    async def email_exists(self, normalized_email: str) -> bool:
        result = await self._session.execute(
            select(User.id).where(User.email == normalized_email).limit(1)
        )
        return result.scalar_one_or_none() is not None

    def add(self, user: User) -> User:
        self._session.add(user)
        return user
