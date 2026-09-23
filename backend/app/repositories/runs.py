"""Run queries.

Ownership reads constrain by ``user_id`` *and* by the conversation still being
live, so a run in a deleted conversation disappears with it.

State transitions are conditional UPDATEs (``WHERE status IN (...)``) rather
than read-modify-write. A run can be finished by its executor, cancelled by an
API call, or expired by the stale sweep, possibly at the same moment; the
condition makes exactly one of them win, and the caller learns from the row
count whether it was the one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.run import ACTIVE_RUN_STATUSES, Run, RunStatus


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, run: Run) -> Run:
        self._session.add(run)
        return run

    async def get_owned(self, *, user_id: uuid.UUID, run_id: uuid.UUID) -> Run | None:
        result = await self._session.execute(
            select(Run)
            .join(Conversation, Conversation.id == Run.conversation_id)
            .where(
                Run.id == run_id,
                Run.user_id == user_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
            # Another session (the executor) may have changed the row since
            # this session last saw it; always read the current values.
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get(self, run_id: uuid.UUID) -> Run | None:
        """Unscoped read, for the executor, which only holds ids it created."""
        result = await self._session.execute(
            select(Run).where(Run.id == run_id).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_by_idempotency_key(self, *, user_id: uuid.UUID, key: str) -> Run | None:
        result = await self._session.execute(
            select(Run)
            .where(Run.user_id == user_id, Run.idempotency_key == key)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_active(self, *, conversation_id: uuid.UUID) -> Run | None:
        result = await self._session.execute(
            select(Run).where(
                Run.conversation_id == conversation_id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        return result.scalar_one_or_none()

    async def expire_stale(
        self, *, conversation_id: uuid.UUID, created_before: datetime, now: datetime
    ) -> int:
        """Fail active runs too old to still be alive.

        A process that crashes mid-run leaves its row ``running`` forever, and
        the one-active-run index would then lock the conversation for good. No
        healthy run outlives the run deadline, so anything older than it plus a
        grace period is dead, whichever replica owned it.
        """
        result = await self._session.execute(
            update(Run)
            .where(
                Run.conversation_id == conversation_id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
                Run.created_at < created_before,
            )
            .values(
                status=RunStatus.failed,
                error_code="run_interrupted",
                error_retryable=True,
                completed_at=now,
            )
            .returning(Run.id)
        )
        return len(result.all())

    async def mark_running(self, *, run_id: uuid.UUID, now: datetime) -> bool:
        result = await self._session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.queued)
            .values(status=RunStatus.running, started_at=now)
            .returning(Run.id)
        )
        return result.first() is not None

    async def finish(self, *, run_id: uuid.UUID, status: RunStatus, **values: Any) -> Run | None:
        """Move an active run to a terminal status.

        Returns the updated run, or None if it had already left the active
        states — someone else finished it first, and their outcome stands.
        """
        assert status not in ACTIVE_RUN_STATUSES
        result = await self._session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.in_(ACTIVE_RUN_STATUSES))
            .values(status=status, **values)
            .returning(Run)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()
