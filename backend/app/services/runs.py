"""Run lifecycle on the request side: accept, replay, read, cancel.

Executing a run is :mod:`app.services.run_executor`'s job. This module decides
whether a run may start at all, and records it. The rules it enforces:

- **Ownership** — every lookup is scoped to the caller; a foreign id is a 404.
- **Idempotency** — ``(user, Idempotency-Key)`` is unique. A repeated key with
  the same request returns the original run instead of creating another; with
  a different request it is rejected.
- **One active run per conversation** — checked first against the run lock
  (:mod:`app.core.run_lock`), which turns a conflict away before any database
  work and coordinates replicas; then enforced by a partial unique index,
  which holds under concurrency no matter how requests interleave, even if
  the lock is lost.
- **Atomic acceptance** — the user message and the run commit together or not
  at all.
- **Spend limits** — a run needs a verified email address, and each user may
  start at most ``DAILY_RUN_LIMIT_PER_USER`` runs in any rolling 24 hours.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import PROMPT_VERSION
from app.api.errors import (
    DailyQuotaExceeded,
    EmailNotVerified,
    IdempotencyKeyReused,
    MessageEmpty,
    MessageNotFound,
    MessageTooLong,
    RetryNotAllowed,
    RunInProgress,
    RunNotFound,
    ServiceUnavailable,
)
from app.core import telemetry
from app.core.config import Settings
from app.core.run_lock import RunLock, RunLockUnavailable
from app.models.conversation import Conversation
from app.models.message import Message, MessageRole
from app.models.run import Run, RunStatus
from app.models.user import User
from app.repositories.citations import CitationRepository
from app.repositories.messages import MessageRepository
from app.repositories.runs import RunRepository
from app.schemas.runs import RunCreate
from app.services.conversations import ConversationService, MessageWithCitations
from app.services.run_executor import RunManager

logger = logging.getLogger(__name__)

# The quota's rolling window.
QUOTA_WINDOW = timedelta(hours=24)

# How long a cancel request waits for the executor to record the outcome, so
# its response can report the final status rather than "still running".
CANCEL_WAIT_SECONDS = 5.0


def normalize_content(raw: str) -> str:
    """Canonical form of a user message.

    Line endings are unified so the same text typed on two platforms is the
    same message, and NUL is removed because PostgreSQL text cannot store it —
    left in, it would surface as a 500 instead of being harmlessly dropped.
    """
    return raw.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()


def request_fingerprint(
    *, conversation_id: uuid.UUID, content: str | None, retry_message_id: uuid.UUID | None
) -> str:
    """What makes two requests "the same request" for idempotency."""
    canonical = json.dumps(
        {
            "conversation_id": str(conversation_id),
            "content": content,
            "retry_message_id": str(retry_message_id) if retry_message_id else None,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AcceptedRun:
    """A new run, committed and ready to execute."""

    run: Run
    user_message: Message


@dataclass(frozen=True, slots=True)
class ReplayedRun:
    """A finished run found under a repeated Idempotency-Key."""

    run: Run
    user_message: Message
    assistant_message: MessageWithCitations | None


@dataclass(frozen=True, slots=True)
class RunView:
    run: Run
    assistant_message: MessageWithCitations | None


class RunService:
    def __init__(self, session: AsyncSession, settings: Settings, run_lock: RunLock) -> None:
        self._session = session
        self._settings = settings
        self._run_lock = run_lock
        self._runs = RunRepository(session)
        self._messages = MessageRepository(session)
        self._citations = CitationRepository(session)
        self._conversations = ConversationService(session)

    # --- create -----------------------------------------------------------

    async def create(
        self,
        *,
        user: User,
        conversation_id: uuid.UUID,
        payload: RunCreate,
        idempotency_key: str,
    ) -> AcceptedRun | ReplayedRun:
        if self._settings.require_email_verification and not user.is_verified:
            raise EmailNotVerified

        # Ownership first: a replay must not reveal anything about a
        # conversation the caller cannot see (or has since deleted).
        conversation = await self._conversations.get(user=user, conversation_id=conversation_id)

        content = normalize_content(payload.content) if payload.content is not None else None
        fingerprint = request_fingerprint(
            conversation_id=conversation_id,
            content=content,
            retry_message_id=payload.retry_message_id,
        )

        existing = await self._runs.get_by_idempotency_key(user_id=user.id, key=idempotency_key)
        if existing is not None:
            return await self._replay(existing, fingerprint)

        if content is not None:
            self._check_length(content)

        # After the replay check: a client retrying a request that was already
        # accepted must get its answer back even if that run used the last
        # unit of quota.
        await self._check_daily_quota(user.id)

        # Plain values, captured before the transaction that can fail: a
        # rollback expires every ORM object in the session, the current user
        # included, and reading an expired attribute would need a lazy reload
        # that async SQLAlchemy does not allow.
        user_id = user.id
        conversation_key = conversation.id

        await self._expire_stale_runs(conversation_key)

        # The run's id is chosen now, before its row exists, so the lock can
        # be taken in its name and released by whoever ends the run.
        run_id = uuid.uuid4()
        await self._take_lock(conversation_key, run_id)
        committed = False
        try:
            accepted = await self._accept(
                run_id=run_id,
                user=user,
                conversation=conversation,
                content=content,
                retry_message_id=payload.retry_message_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            await self._session.commit()
            committed = True
        except IntegrityError:
            # The lock let us through, but a unique index refused: the
            # idempotency key (the same key sent at once to two conversations,
            # which hold separate locks), the one-active-run index (the lock
            # was lost or expired — the database is the source of truth), or
            # the message sequence. Rolling back discards our user message as
            # well as our run.
            await self._session.rollback()
            existing = await self._runs.get_by_idempotency_key(user_id=user_id, key=idempotency_key)
            if existing is not None:
                return await self._replay(existing, fingerprint)
            raise await self._in_progress(conversation_key) from None
        finally:
            if not committed:
                # Every path that did not create the run gives the lock back:
                # a conflict, a rejected retry, a client that hung up mid-way.
                # Shielded so a cancelled request still completes the release.
                await asyncio.shield(self._run_lock.release(conversation_key, run_id))

        logger.info(
            "run_accepted",
            extra={
                "run_id": accepted.run.id,
                "conversation_id": conversation_key,
                "retry": payload.retry_message_id is not None,
            },
        )
        return accepted

    async def _take_lock(self, conversation_id: uuid.UUID, run_id: uuid.UUID) -> None:
        try:
            taken = await self._run_lock.acquire(conversation_id, run_id)
        except RunLockUnavailable:
            # Fail closed. The database index would still prevent a second
            # run, but production is configured to coordinate through Redis,
            # and silently running without it hides an outage.
            logger.error("run_lock_unavailable", extra={"conversation_id": conversation_id})
            raise ServiceUnavailable from None
        if not taken:
            logger.info("run_rejected_locked", extra={"conversation_id": conversation_id})
            raise await self._in_progress(conversation_id)

    async def _check_daily_quota(self, user_id: uuid.UUID) -> None:
        """Refuse a run beyond the user's rolling 24-hour allowance.

        A check-then-insert, so two submissions racing in different
        conversations can each pass at the boundary and overshoot by the
        number in flight. That is bounded by the per-minute limit and is cheap
        next to the cost of serialising every submission a user makes.
        """
        limit = self._settings.daily_run_limit_per_user
        now = datetime.now(UTC)
        count, oldest = await self._runs.count_started_since(
            user_id=user_id, since=now - QUOTA_WINDOW
        )
        if count < limit:
            return
        # A slot frees up when the oldest run in the window ages out of it.
        retry_after = (
            max(1, math.ceil((oldest + QUOTA_WINDOW - now).total_seconds()))
            if oldest is not None
            else 1
        )
        telemetry.record_rate_limited("daily_quota")
        logger.info("run_rejected_daily_quota", extra={"limit": limit})
        raise DailyQuotaExceeded(
            f"You have reached the limit of {limit} messages in 24 hours.",
            headers={"Retry-After": str(retry_after)},
            details={"limit": limit, "retry_after_seconds": retry_after},
        )

    def _check_length(self, content: str) -> None:
        if not content:
            raise MessageEmpty
        limit = self._settings.max_user_message_chars
        if len(content) > limit:
            raise MessageTooLong(
                f"The message is longer than {limit} characters.",
                details={"max_chars": limit, "chars": len(content)},
            )

    async def _accept(
        self,
        *,
        run_id: uuid.UUID,
        user: User,
        conversation: Conversation,
        content: str | None,
        retry_message_id: uuid.UUID | None,
        idempotency_key: str,
        fingerprint: str,
    ) -> AcceptedRun:
        if content is not None:
            user_message = await self._conversations.stage_message(
                conversation=conversation, role=MessageRole.user, content=content
            )
        else:
            assert retry_message_id is not None
            user_message = await self._retry_target(user, conversation, retry_message_id)

        now = datetime.now(UTC)
        conversation.updated_at = now
        # Flush the message first: the run's foreign key needs its row.
        await self._session.flush()
        run = self._runs.add(
            Run(
                id=run_id,
                user_id=user.id,
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                status=RunStatus.queued,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                provider=self._settings.llm_provider,
                model=self._settings.llm_model,
                prompt_version=PROMPT_VERSION,
                search_count=0,
            )
        )
        await self._session.flush()
        await self._session.refresh(run)
        return AcceptedRun(run=run, user_message=user_message)

    async def _retry_target(
        self, user: User, conversation: Conversation, message_id: uuid.UUID
    ) -> Message:
        """The user message a retry answers again.

        Only the latest message qualifies. Retrying an older one would produce
        an answer that sits below later turns it never saw — a thread that
        reads out of order.
        """
        message = await self._messages.get_owned(
            user_id=user.id, conversation_id=conversation.id, message_id=message_id
        )
        if message is None or message.role is not MessageRole.user:
            raise MessageNotFound
        latest = await self._messages.latest_sequence_number(conversation_id=conversation.id)
        if message.sequence_number != latest:
            raise RetryNotAllowed
        return message

    async def _replay(self, run: Run, fingerprint: str) -> ReplayedRun:
        if run.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused
        if run.is_active:
            # Like an idempotent API mid-request: the original is still being
            # processed, so say so, and point at the recovery endpoint.
            raise RunInProgress(
                "The request with this Idempotency-Key is still being processed.",
                details={"run_id": str(run.id)},
            )
        user_message = await self._messages.get(run.user_message_id)
        assert user_message is not None  # FK-guaranteed
        return ReplayedRun(
            run=run,
            user_message=user_message,
            assistant_message=await self._assistant_message(run),
        )

    async def _in_progress(self, conversation_id: uuid.UUID) -> RunInProgress:
        active = await self._runs.get_active(conversation_id=conversation_id)
        # A committed run is the better answer. Failing that, the lock names a
        # run that is being accepted right now, before its row is committed.
        run_id = active.id if active else await self._run_lock.holder(conversation_id)
        return RunInProgress(details={"run_id": str(run_id)} if run_id else None)

    async def _expire_stale_runs(self, conversation_id: uuid.UUID) -> None:
        now = datetime.now(UTC)
        threshold = now - timedelta(
            seconds=self._settings.run_timeout_seconds + self._settings.run_stale_grace_seconds
        )
        expired = await self._runs.expire_stale(
            conversation_id=conversation_id, created_before=threshold, now=now
        )
        for run_id in expired:
            # Its lease has normally expired by now too; releasing makes that
            # true even if replicas disagree on the configured timeouts.
            await self._run_lock.release(conversation_id, run_id)
        if expired:
            logger.warning(
                "run_expired_stale",
                extra={"conversation_id": conversation_id, "count": len(expired)},
            )

    # --- read -------------------------------------------------------------

    async def get(self, *, user: User, run_id: uuid.UUID) -> RunView:
        run = await self._runs.get_owned(user_id=user.id, run_id=run_id)
        if run is None:
            raise RunNotFound
        if run.is_active:
            # A poller must not wait forever on a run whose process died.
            await self._expire_stale_runs(run.conversation_id)
            await self._session.commit()
            run = await self._runs.get_owned(user_id=user.id, run_id=run_id)
            assert run is not None
        return RunView(run=run, assistant_message=await self._assistant_message(run))

    async def _assistant_message(self, run: Run) -> MessageWithCitations | None:
        if run.assistant_message_id is None:
            return None
        message = await self._messages.get(run.assistant_message_id)
        if message is None:
            return None
        citations = await self._citations.for_messages([message.id])
        return MessageWithCitations(message, citations.get(message.id, []))

    # --- cancel -----------------------------------------------------------

    async def cancel(self, *, user: User, run_id: uuid.UUID, manager: RunManager) -> RunView:
        """Stop a run. Idempotent: cancelling a finished run reports how it ended.

        Two mechanisms, because the run may or may not be executing in this
        process. Locally, the executor's task is cancelled — which interrupts
        an in-flight model stream at once — and it records the outcome itself.
        Either way the database is then told directly, conditioned on the run
        still being active, so a run owned by another replica (or by a process
        that died) is stopped as well; its executor will find the row no longer
        active and discard its result.
        """
        view = await self.get(user=user, run_id=run_id)
        if not view.run.is_active:
            return view

        handle = manager.cancel(run_id, reason="user_cancelled")
        if handle is not None:
            await manager.wait(handle, grace_seconds=CANCEL_WAIT_SECONDS)
        else:
            # Executing on another replica, if anywhere: tell it to stop now.
            await manager.signal_cancel(reason="user_cancelled", run_id=run_id)

        await self._runs.finish(
            run_id=run_id,
            status=RunStatus.cancelled,
            error_code="user_cancelled",
            completed_at=datetime.now(UTC),
        )
        await self._session.commit()
        # The executor releases the lock when it ends, but it may be on another
        # replica and take a while to notice. The run is over as of this
        # commit, so the conversation is free now. Naming the run makes this a
        # no-op if the executor got there first and a new run took the lock.
        await self._run_lock.release(view.run.conversation_id, run_id)
        return await self.get(user=user, run_id=run_id)
