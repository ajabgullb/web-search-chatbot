"""Message queries.

Reads join ``conversations`` and constrain by ``user_id`` even though services
resolve the conversation first. The join costs almost nothing and means a
future caller cannot reach another user's messages by passing a conversation id
it never verified.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message, MessageRole

# Roles the public API serializes. System prompts and tool payloads stay
# internal: exposing them would leak prompt content and raw provider output.
PUBLIC_ROLES = (MessageRole.user, MessageRole.assistant)


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, message: Message) -> Message:
        self._session.add(message)
        return message

    async def list_page(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        limit: int,
        before_sequence: int | None = None,
    ) -> Sequence[Message]:
        """A page of the thread, oldest-first, walking backwards from the end.

        Chat history is read from the bottom: the client wants the most recent
        turns first and pages *backwards* into older ones, which is why the
        cursor is ``before``. The query selects descending to use the unique
        index, and the caller reverses the page for display.
        """
        statement = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.conversation_id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                Message.role.in_(PUBLIC_ROLES),
            )
            .order_by(Message.sequence_number.desc())
            .limit(limit + 1)
        )
        if before_sequence is not None:
            statement = statement.where(Message.sequence_number < before_sequence)

        result = await self._session.execute(statement)
        return result.scalars().all()

    async def next_sequence_number(self, *, conversation_id: uuid.UUID) -> int:
        """The next free slot in the thread.

        Read-then-write races here are resolved by the unique constraint on
        (conversation_id, sequence_number): the loser gets an IntegrityError
        rather than a silently reordered thread. Phase 6 adds the single-active-
        run lock that stops two writers reaching this point at all.
        """
        result = await self._session.execute(
            select(func.coalesce(func.max(Message.sequence_number), 0)).where(
                Message.conversation_id == conversation_id
            )
        )
        return int(result.scalar_one()) + 1
