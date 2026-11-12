"""LLM credential queries (ADR-0020). Every one is scoped by the owner's id."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_credential import LLMCredential


class LLMCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, *, user_id: uuid.UUID) -> Sequence[LLMCredential]:
        """Newest-checked first, so a fallback picks the freshest key."""
        result = await self._session.execute(
            select(LLMCredential)
            .where(LLMCredential.user_id == user_id)
            .order_by(LLMCredential.verified_at.desc(), LLMCredential.id)
        )
        return result.scalars().all()

    async def get(self, *, user_id: uuid.UUID, provider: str) -> LLMCredential | None:
        result = await self._session.execute(
            select(LLMCredential).where(
                LLMCredential.user_id == user_id, LLMCredential.provider == provider
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        encrypted_api_key: bytes,
        key_hint: str,
        verified_at: datetime,
    ) -> None:
        """Insert, or replace the user's key for this provider.

        One statement, so two saves racing from two tabs cannot both insert
        and leave the loser with a unique-violation 500: the later write wins.
        """
        values = {
            "model": model,
            "encrypted_api_key": encrypted_api_key,
            "key_hint": key_hint,
            "verified_at": verified_at,
        }
        statement = insert(LLMCredential).values(
            id=uuid.uuid4(), user_id=user_id, provider=provider, is_active=False, **values
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_llm_credentials_user_id_provider",
            set_={**values, "updated_at": func.now()},
        )
        await self._session.execute(statement)

    async def activate(
        self, *, user_id: uuid.UUID, provider: str | None, model: str | None
    ) -> bool:
        """Make ``provider``'s key, with ``model``, the active one; None for none.

        Two statements, in this order: PostgreSQL checks a unique index row by
        row, so activating first would briefly hold two active rows and fail.
        A concurrent activation blocks on the first's row locks and then sees
        its result, so the partial index is never violated. Returns False if
        the provider has no key.
        """
        await self._session.execute(
            update(LLMCredential)
            .where(LLMCredential.user_id == user_id, LLMCredential.is_active)
            .values(is_active=False)
        )
        if provider is None:
            return True
        result = await self._session.execute(
            update(LLMCredential)
            .where(LLMCredential.user_id == user_id, LLMCredential.provider == provider)
            .values(is_active=True, model=model, updated_at=func.now())
        )
        return bool(getattr(result, "rowcount", 0))

    async def delete(self, *, user_id: uuid.UUID, provider: str) -> bool:
        result = await self._session.execute(
            delete(LLMCredential).where(
                LLMCredential.user_id == user_id, LLMCredential.provider == provider
            )
        )
        return bool(getattr(result, "rowcount", 0))
