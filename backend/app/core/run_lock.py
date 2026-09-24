"""Application-level lock: at most one active run per conversation.

The database already enforces the rule with a partial unique index, and stays
the source of truth. This lock sits in front of it for two reasons:

- **Rejection before work.** A conflicting submission is turned away before it
  opens a transaction, stages a user message, and has it rolled back by an
  IntegrityError.
- **One view across replicas.** Every API process consults the same Redis key,
  so coordination does not depend on which replica a request lands on.

The lock is a *lease*, not a mutex held by a connection. Its value is the run
id, which is known before the run row exists (ids are generated application
side), so any process can release it on the run's behalf — the executor when
the run ends, a cancel request on another replica, the stale-run sweep — and a
release naming the wrong run is a no-op. Its expiry bounds the damage from a
process that dies while holding it: no longer than the stale-run sweep already
takes to free a conversation whose run died in the database.

If the lock is lost (Redis restarted, a lease expired early), nothing unsafe
happens: the database index still rejects the second run.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Delete the key only if it still names this run. GET-then-DEL from Python
# would race: the lease could expire and be taken by a new run between the two
# calls, and the DEL would then free *that* run's lock. A script runs
# atomically on the server.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RunLockUnavailable(Exception):
    """The lock backend could not be reached, so the lock state is unknown."""


class RunLock(Protocol):
    async def acquire(self, conversation_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        """Take the conversation's lease for ``run_id``. False if another run holds it.

        Raises :class:`RunLockUnavailable` when the answer is unknown.
        """
        ...

    async def release(self, conversation_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Free the lease if ``run_id`` still holds it. Never raises."""
        ...

    async def holder(self, conversation_id: uuid.UUID) -> uuid.UUID | None:
        """The run holding the lease, if any and if known. Never raises."""
        ...


def _key(conversation_id: uuid.UUID) -> str:
    return f"run-lock:conversation:{conversation_id}"


class RedisRunLock:
    """The lease in Redis, shared by every API replica."""

    def __init__(self, client: Redis, *, lease_seconds: float) -> None:
        self._client = client
        self._lease_ms = int(lease_seconds * 1000)
        self._release = client.register_script(_RELEASE_SCRIPT)

    async def acquire(self, conversation_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        try:
            # SET NX PX is a single atomic step: check-and-take plus the expiry.
            # Setting the expiry separately would leave a window where a crash
            # leaves a lease that never expires.
            taken = await self._client.set(
                _key(conversation_id), str(run_id), nx=True, px=self._lease_ms
            )
        except RedisError as exc:
            raise RunLockUnavailable from exc
        return bool(taken)

    async def release(self, conversation_id: uuid.UUID, run_id: uuid.UUID) -> None:
        try:
            await self._release(keys=[_key(conversation_id)], args=[str(run_id)])
        except RedisError:
            # Callers release from cleanup paths that must not fail. The lease
            # expires on its own, and the database index keeps things correct
            # meanwhile.
            logger.warning(
                "run_lock_release_failed",
                extra={"conversation_id": conversation_id, "run_id": run_id},
            )

    async def holder(self, conversation_id: uuid.UUID) -> uuid.UUID | None:
        try:
            value = await self._client.get(_key(conversation_id))
        except RedisError:
            return None
        if value is None:
            return None
        return uuid.UUID(value.decode() if isinstance(value, bytes) else str(value))


def build_run_lock(client: Redis | None, *, lease_seconds: float) -> RunLock:
    if client is None:
        return InMemoryRunLock(lease_seconds=lease_seconds)
    return RedisRunLock(client, lease_seconds=lease_seconds)


class InMemoryRunLock:
    """The same lease in process memory, for tests and single-process development.

    Correct for one process only. Production requires Redis (see
    ``Settings._production_fails_closed``), so this never coordinates replicas.
    """

    def __init__(self, *, lease_seconds: float) -> None:
        self._lease_seconds = lease_seconds
        self._leases: dict[uuid.UUID, tuple[uuid.UUID, float]] = {}

    async def acquire(self, conversation_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        now = time.monotonic()
        held = self._leases.get(conversation_id)
        if held is not None and held[1] > now:
            return False
        self._leases[conversation_id] = (run_id, now + self._lease_seconds)
        return True

    async def release(self, conversation_id: uuid.UUID, run_id: uuid.UUID) -> None:
        held = self._leases.get(conversation_id)
        if held is not None and held[0] == run_id:
            del self._leases[conversation_id]

    async def holder(self, conversation_id: uuid.UUID) -> uuid.UUID | None:
        held = self._leases.get(conversation_id)
        if held is None or held[1] <= time.monotonic():
            return None
        return held[0]
