"""Message queries.

Reads join ``conversations`` and constrain by ``user_id`` even though services
resolve the conversation first. The join costs almost nothing and means a
future caller cannot reach another user's messages by passing a conversation id
it never verified.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message, MessageRole, MessageStatus

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
        rather than a silently reordered thread. The single-active-run index on
        ``runs`` stops two writers reaching this point at all.
        """
        result = await self._session.execute(
            select(func.coalesce(func.max(Message.sequence_number), 0)).where(
                Message.conversation_id == conversation_id
            )
        )
        return int(result.scalar_one()) + 1

    async def latest_sequence_number(self, *, conversation_id: uuid.UUID) -> int:
        return await self.next_sequence_number(conversation_id=conversation_id) - 1

    async def get_owned(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID
    ) -> Message | None:
        result = await self._session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get(self, message_id: uuid.UUID) -> Message | None:
        """Unscoped read, for ids taken from an already ownership-checked run."""
        return await self._session.get(Message, message_id)

    async def history(
        self, *, conversation_id: uuid.UUID, up_to_sequence: int, limit: int
    ) -> list[Message]:
        """The newest ``limit`` turns the model should see, oldest first.

        Every user turn counts — the user did say it, even if no answer came.
        Assistant turns count only when complete: a failed or cancelled
        fragment is not something the model should build on.
        """
        result = await self._session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.sequence_number <= up_to_sequence,
                Message.role.in_(PUBLIC_ROLES),
                or_(
                    Message.role == MessageRole.user,
                    Message.status == MessageStatus.complete,
                ),
            )
            .order_by(Message.sequence_number.desc())
            .limit(max(limit, 1))
        )
        return list(reversed(result.scalars().all()))
