"""Conversation and message endpoints.

Handlers stay thin: parse, delegate, serialize. Ownership is not checked here —
it is a property of every query the service issues, so there is no path through
these functions that could skip it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.api.dependencies import ConversationServiceDep, CurrentUserDep, RunManagerDep
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetail,
    ConversationRead,
    ConversationUpdate,
)
from app.schemas.message import MessageRead
from app.schemas.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=Page[ConversationRead])
async def list_conversations(
    current_user: CurrentUserDep,
    service: ConversationServiceDep,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> Page[ConversationRead]:
    page = await service.list(user=current_user, limit=limit, cursor=cursor)
    return Page[ConversationRead](
        items=[ConversationRead.model_validate(row) for row in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post("", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    current_user: CurrentUserDep,
    service: ConversationServiceDep,
) -> ConversationRead:
    conversation = await service.create(user=current_user, title=payload.title)
    return ConversationRead.model_validate(conversation)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: ConversationServiceDep,
) -> ConversationDetail:
    conversation = await service.get(user=current_user, conversation_id=conversation_id)
    detail = ConversationDetail.model_validate(conversation)
    detail.active_run_id = await service.active_run_id(conversation=conversation)
    return detail


@router.patch("/{conversation_id}", response_model=ConversationRead)
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    current_user: CurrentUserDep,
    service: ConversationServiceDep,
) -> ConversationRead:
    conversation = await service.rename(
        user=current_user, conversation_id=conversation_id, title=payload.title
    )
    return ConversationRead.model_validate(conversation)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: ConversationServiceDep,
    manager: RunManagerDep,
) -> None:
    await service.delete(user=current_user, conversation_id=conversation_id)
    # An answer being written into a deleted conversation is work nobody can
    # ever see; stop it, here and on whichever replica is writing it.
    manager.cancel_conversation(conversation_id, reason="conversation_deleted")
    await manager.signal_cancel(reason="conversation_deleted", conversation_id=conversation_id)


@router.get("/{conversation_id}/messages", response_model=Page[MessageRead])
async def list_messages(
    conversation_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: ConversationServiceDep,
    before: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> Page[MessageRead]:
    page = await service.list_messages(
        user=current_user, conversation_id=conversation_id, limit=limit, before=before
    )
    return Page[MessageRead](
        items=[MessageRead.build(row.message, row.citations) for row in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )
