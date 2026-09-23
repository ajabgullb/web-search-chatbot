"""Conversation queries.

Every method takes ``user_id`` and puts it in the WHERE clause. That is
deliberate and is the whole authorization story for this table: there is no
"fetch by id" a service could call and then forget to check. A row belonging to
someone else is simply not returned, so the caller raises the same 404 it would
for an id that never existed — a probing client learns nothing either way.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, DateTime, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, conversation: Conversation) -> Conversation:
        self._session.add(conversation)
        return conversation

    async def get(self, *, user_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation | None:
        """Return a live conversation the user owns, or None."""
        result = await self._session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_page(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> Sequence[Conversation]:
        """Newest-first page of the user's conversations.

        ``after`` is the (updated_at, id) of the last row the client saw. The
        row-value comparison asks PostgreSQL for everything ordered strictly
        below it, which matches the index exactly and cannot repeat or skip a
        row when another conversation is created or renamed mid-listing.

        Fetches one extra row so the caller can tell "more to come" from "this
        page happens to be full" without a second COUNT query.
        """
        statement = (
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.deleted_at.is_(None))
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit + 1)
        )
        if after is not None:
            # Bind the cursor values with explicit types: asyncpg has to send a
            # type for every parameter, and an untyped timestamp/uuid pair in a
            # row comparison is exactly where it guesses wrong.
            cursor_updated_at, cursor_id = after
            statement = statement.where(
                tuple_(Conversation.updated_at, Conversation.id)
                < tuple_(
                    literal(cursor_updated_at, DateTime(timezone=True)),
                    literal(cursor_id, PgUUID(as_uuid=True)),
                )
            )

        result = await self._session.execute(statement)
        return result.scalars().all()

    async def touch(self, *, conversation_id: uuid.UUID, at: datetime) -> None:
        """Bump ``updated_at`` so an active thread rises to the top of the list."""
        await self._session.execute(
            update(Conversation).where(Conversation.id == conversation_id).values(updated_at=at)
        )

    async def soft_delete(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, at: datetime
    ) -> bool:
        """Mark the conversation deleted. Returns False if it was not the user's.

        Stamping ``deleted_at`` rather than issuing a DELETE keeps message rows
        and, from Phase 6, run history intact for audit while removing the
        conversation from every read path.
        """
        result = await self._session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
            .values(deleted_at=at)
        )
        # An UPDATE always yields a CursorResult; the generic Result protocol
        # that execute() is annotated with does not expose rowcount.
        return cast("CursorResult[Any]", result).rowcount > 0
