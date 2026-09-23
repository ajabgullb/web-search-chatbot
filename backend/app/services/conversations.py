"""Conversation and message orchestration.

Route handlers call into here; SQL stays in the repositories. Everything takes
the authenticated ``User`` rather than a bare id, so a handler cannot
accidentally pass the id from the URL where the caller's id belongs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConversationNotFound, InvalidCursor
from app.core.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.models.citation import Citation
from app.models.conversation import Conversation
from app.models.message import Message, MessageRole, MessageStatus
from app.models.user import User
from app.repositories.citations import CitationRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository

DEFAULT_TITLE = "New conversation"
# Shorter than the column so a derived title always fits, and short enough to
# read in a narrow sidebar without truncation by CSS alone.
TITLE_MAX_LENGTH = 80


def derive_title(content: str) -> str:
    """Build a conversation title from the first user message.

    Deterministic and local on purpose: titling must never sit on the critical
    path of the first answer, and an LLM call here would add latency and cost
    to every new thread for a cosmetic string. AGENTS.md §10 allows an optional
    model-generated title later; this is the v1 behaviour.
    """
    # split() with no argument collapses runs of any whitespace, so a pasted
    # multi-line prompt becomes one clean line.
    collapsed = " ".join(content.split())
    if not collapsed:
        return DEFAULT_TITLE
    if len(collapsed) <= TITLE_MAX_LENGTH:
        return collapsed

    # Cut on a word boundary when there is one reasonably close to the limit,
    # so the result reads as a phrase rather than a severed word.
    head = collapsed[: TITLE_MAX_LENGTH - 1]
    boundary = head.rfind(" ")
    if boundary >= TITLE_MAX_LENGTH // 2:
        head = head[:boundary]
    return f"{head.rstrip()}…"


def normalize_title(title: str) -> str:
    """Tidy a user-supplied title without rejecting it."""
    collapsed = " ".join(title.split())
    return collapsed or DEFAULT_TITLE


@dataclass(frozen=True, slots=True)
class PageResult[T]:
    """Rows plus the cursor that continues the listing."""

    items: list[T]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True, slots=True)
class MessageWithCitations:
    message: Message
    citations: list[Citation]


def _encode_conversation_cursor(conversation: Conversation) -> str:
    return encode_cursor(conversation.updated_at.isoformat(), str(conversation.id))


def _decode_conversation_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        updated_at, raw_id = decode_cursor(cursor, fields=2)
        return datetime.fromisoformat(updated_at), uuid.UUID(raw_id)
    except (InvalidCursorError, ValueError) as exc:
        raise InvalidCursor from exc


def _decode_message_cursor(cursor: str) -> int:
    try:
        (sequence,) = decode_cursor(cursor, fields=1)
        return int(sequence)
    except (InvalidCursorError, ValueError) as exc:
        raise InvalidCursor from exc


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._citations = CitationRepository(session)

    async def create(self, *, user: User, title: str | None = None) -> Conversation:
        conversation = Conversation(
            user_id=user.id,
            title=normalize_title(title) if title is not None else DEFAULT_TITLE,
        )
        self._conversations.add(conversation)
        await self._session.commit()
        await self._session.refresh(conversation)
        return conversation

    async def get(self, *, user: User, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self._conversations.get(
            user_id=user.id, conversation_id=conversation_id
        )
        if conversation is None:
            # 404 rather than 403, for a conversation that exists but belongs to
            # someone else as much as for one that never existed. A 403 would
            # confirm the id is real, turning the endpoint into an oracle.
            raise ConversationNotFound
        return conversation

    async def list(
        self, *, user: User, limit: int, cursor: str | None = None
    ) -> PageResult[Conversation]:
        after = _decode_conversation_cursor(cursor) if cursor else None
        rows = list(await self._conversations.list_page(user_id=user.id, limit=limit, after=after))

        # The repository fetched limit+1; the extra row is the proof that
        # another page exists, and is not itself returned.
        overflow = len(rows) > limit
        items = rows[:limit]
        next_cursor = _encode_conversation_cursor(items[-1]) if overflow and items else None
        return PageResult(items=items, next_cursor=next_cursor)

    async def rename(self, *, user: User, conversation_id: uuid.UUID, title: str) -> Conversation:
        conversation = await self.get(user=user, conversation_id=conversation_id)
        conversation.title = normalize_title(title)
        await self._session.commit()
        await self._session.refresh(conversation)
        return conversation

    async def delete(self, *, user: User, conversation_id: uuid.UUID) -> None:
        deleted = await self._conversations.soft_delete(
            user_id=user.id,
            conversation_id=conversation_id,
            at=datetime.now(UTC),
        )
        if not deleted:
            raise ConversationNotFound
        await self._session.commit()

    async def list_messages(
        self, *, user: User, conversation_id: uuid.UUID, limit: int, before: str | None = None
    ) -> PageResult[MessageWithCitations]:
        # Resolve the conversation first so an unknown or unowned id fails the
        # same way whether or not it has messages.
        await self.get(user=user, conversation_id=conversation_id)

        before_sequence = _decode_message_cursor(before) if before else None
        rows = list(
            await self._messages.list_page(
                user_id=user.id,
                conversation_id=conversation_id,
                limit=limit,
                before_sequence=before_sequence,
            )
        )

        overflow = len(rows) > limit
        page = rows[:limit]
        # The query walks backwards from the newest turn; the client renders
        # a thread top to bottom, so hand back the page in reading order.
        next_cursor = encode_cursor(str(page[-1].sequence_number)) if overflow and page else None
        page.reverse()
        citations = await self._citations.for_messages([message.id for message in page])
        return PageResult(
            items=[MessageWithCitations(m, citations.get(m.id, [])) for m in page],
            next_cursor=next_cursor,
        )

    async def append_message(
        self,
        *,
        user: User,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str,
        status: MessageStatus = MessageStatus.complete,
    ) -> Message:
        """Add a turn and commit it."""
        conversation = await self.get(user=user, conversation_id=conversation_id)
        message = await self.stage_message(
            conversation=conversation, role=role, content=content, status=status
        )
        await self._session.commit()
        await self._session.refresh(message)
        return message

    async def stage_message(
        self,
        *,
        conversation: Conversation,
        role: MessageRole,
        content: str,
        status: MessageStatus = MessageStatus.complete,
    ) -> Message:
        """Add a turn to the session without committing.

        The run service writes the user's message and the run that answers it
        in one transaction, so a rejected run never leaves an orphaned message
        behind. It lives here rather than in the run service because the title
        rule belongs to the conversation.
        """
        sequence = await self._messages.next_sequence_number(conversation_id=conversation.id)
        message = Message(
            conversation_id=conversation.id,
            role=role,
            content=content,
            status=status,
            sequence_number=sequence,
        )
        self._messages.add(message)

        # Title on the first user turn only: later edits by the user must not
        # be silently overwritten by message two.
        if sequence == 1 and role is MessageRole.user and conversation.title == DEFAULT_TITLE:
            conversation.title = derive_title(content)
        return message
