"""The run lock's contract, held by both implementations.

Every behavioural test runs against Redis (a fakeredis server, which executes
the real commands and the Lua release script) and the in-memory lock, so the
development stand-in cannot drift from what production does.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import fakeredis
import pytest

from app.core.run_lock import InMemoryRunLock, RedisRunLock, RunLock, RunLockUnavailable

LEASE_SECONDS = 30.0


@pytest.fixture(params=["redis", "memory"])
async def lock(request: pytest.FixtureRequest) -> AsyncIterator[RunLock]:
    if request.param == "memory":
        yield InMemoryRunLock(lease_seconds=LEASE_SECONDS)
        return
    client = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
    yield RedisRunLock(client, lease_seconds=LEASE_SECONDS)
    await client.aclose()


def ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


async def test_one_run_holds_a_conversation_at_a_time(lock: RunLock) -> None:
    conversation, first, second = ids()

    assert await lock.acquire(conversation, first) is True
    assert await lock.acquire(conversation, second) is False
    assert await lock.holder(conversation) == first


async def test_release_frees_the_conversation_for_the_next_run(lock: RunLock) -> None:
    conversation, first, second = ids()
    await lock.acquire(conversation, first)

    await lock.release(conversation, first)

    assert await lock.holder(conversation) is None
    assert await lock.acquire(conversation, second) is True


async def test_a_release_naming_another_run_changes_nothing(lock: RunLock) -> None:
    """A late release, say from a cancel on another replica after the run
    already ended and a new one started, must not free the new run's lock."""
    conversation, old_run, new_run = ids()
    await lock.acquire(conversation, new_run)

    await lock.release(conversation, old_run)

    assert await lock.holder(conversation) == new_run
    assert await lock.acquire(conversation, old_run) is False


async def test_releasing_twice_is_harmless(lock: RunLock) -> None:
    conversation, run, _ = ids()
    await lock.acquire(conversation, run)

    await lock.release(conversation, run)
    await lock.release(conversation, run)

    assert await lock.holder(conversation) is None


async def test_conversations_are_locked_independently(lock: RunLock) -> None:
    one, two, run = ids()

    assert await lock.acquire(one, run) is True
    assert await lock.acquire(two, uuid.uuid4()) is True


@pytest.mark.parametrize("kind", ["redis", "memory"])
async def test_an_unreleased_lease_expires(kind: str) -> None:
    """A process that dies holding the lock must not block the conversation
    for longer than the lease."""
    lease: RunLock
    if kind == "memory":
        lease = InMemoryRunLock(lease_seconds=0.05)
    else:
        lease = RedisRunLock(
            fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()), lease_seconds=0.05
        )
    conversation, crashed, next_run = ids()
    await lease.acquire(conversation, crashed)

    await asyncio.sleep(0.15)

    assert await lease.holder(conversation) is None
    assert await lease.acquire(conversation, next_run) is True


async def test_simultaneous_acquires_admit_exactly_one(lock: RunLock) -> None:
    conversation = uuid.uuid4()
    results = await asyncio.gather(*(lock.acquire(conversation, uuid.uuid4()) for _ in range(20)))
    assert results.count(True) == 1


class TestRedisOutage:
    @pytest.fixture
    def outage(self) -> tuple[RedisRunLock, fakeredis.FakeServer]:
        server = fakeredis.FakeServer()
        lock = RedisRunLock(fakeredis.FakeAsyncRedis(server=server), lease_seconds=LEASE_SECONDS)
        server.connected = False
        return lock, server

    async def test_acquire_reports_the_outage_instead_of_guessing(
        self, outage: tuple[RedisRunLock, fakeredis.FakeServer]
    ) -> None:
        """Answering True would skip the check; False would block every
        conversation. Neither is honest, so it raises."""
        lock, _ = outage
        with pytest.raises(RunLockUnavailable):
            await lock.acquire(uuid.uuid4(), uuid.uuid4())

    async def test_release_and_holder_never_raise(
        self, outage: tuple[RedisRunLock, fakeredis.FakeServer]
    ) -> None:
        """They run in cleanup paths; the lease expires on its own."""
        lock, _ = outage
        await lock.release(uuid.uuid4(), uuid.uuid4())
        assert await lock.holder(uuid.uuid4()) is None
