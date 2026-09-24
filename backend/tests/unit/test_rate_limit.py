"""Rate limiters: one sliding-window contract, two implementations.

The behavioural tests run against Redis (fakeredis, which executes the real
Lua script) and the in-process limiter, so development cannot drift from
production.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import fakeredis
import pytest

from app.core.rate_limit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter


@pytest.fixture(params=["redis", "memory"])
async def limiter(request: pytest.FixtureRequest) -> AsyncIterator[RateLimiter]:
    if request.param == "memory":
        yield InMemoryRateLimiter()
        return
    client = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
    yield RedisRateLimiter(client)
    await client.aclose()


async def test_allows_up_to_the_limit_then_blocks(limiter: RateLimiter) -> None:
    allowed = [(await limiter.hit("k", limit=3, window_seconds=60)).allowed for _ in range(3)]
    blocked = await limiter.hit("k", limit=3, window_seconds=60)

    assert allowed == [True, True, True]
    assert blocked.allowed is False
    # Most of the window is still ahead: the oldest attempt was just now.
    assert 55 <= blocked.retry_after_seconds <= 60


async def test_buckets_are_independent(limiter: RateLimiter) -> None:
    for _ in range(3):
        await limiter.hit("a", limit=3, window_seconds=60)

    assert (await limiter.hit("a", limit=3, window_seconds=60)).allowed is False
    assert (await limiter.hit("b", limit=3, window_seconds=60)).allowed is True


async def test_entries_leave_the_window(limiter: RateLimiter) -> None:
    """A zero-length window means nothing is ever still inside it."""
    for _ in range(10):
        assert (await limiter.hit("k", limit=1, window_seconds=0)).allowed is True


async def test_blocked_attempts_do_not_extend_the_ban(limiter: RateLimiter) -> None:
    """Only allowed attempts are recorded. Otherwise a client that keeps
    retrying would never get out of the window."""
    await limiter.hit("k", limit=1, window_seconds=1)
    for _ in range(5):
        assert (await limiter.hit("k", limit=1, window_seconds=1)).allowed is False

    await asyncio.sleep(1.1)

    assert (await limiter.hit("k", limit=1, window_seconds=1)).allowed is True


async def test_simultaneous_hits_never_exceed_the_limit(limiter: RateLimiter) -> None:
    results = await asyncio.gather(
        *(limiter.hit("k", limit=5, window_seconds=60) for _ in range(30))
    )
    assert [r.allowed for r in results].count(True) == 5


class TestRedisAcrossReplicas:
    async def test_replicas_share_one_window(self) -> None:
        """The reason Redis is required in production: with per-process
        counters, N replicas would allow N x the limit."""
        server = fakeredis.FakeServer()
        replica_a = RedisRateLimiter(fakeredis.FakeAsyncRedis(server=server))
        replica_b = RedisRateLimiter(fakeredis.FakeAsyncRedis(server=server))

        assert (await replica_a.hit("login:1.2.3.4", limit=2, window_seconds=60)).allowed
        assert (await replica_b.hit("login:1.2.3.4", limit=2, window_seconds=60)).allowed
        assert not (await replica_a.hit("login:1.2.3.4", limit=2, window_seconds=60)).allowed
        assert not (await replica_b.hit("login:1.2.3.4", limit=2, window_seconds=60)).allowed

    async def test_an_idle_key_expires_from_redis(self) -> None:
        server = fakeredis.FakeServer()
        client = fakeredis.FakeAsyncRedis(server=server)
        await RedisRateLimiter(client).hit("k", limit=5, window_seconds=60)

        ttl_ms = await client.pttl("rate-limit:k")

        assert 0 < ttl_ms <= 60_000

    async def test_an_outage_degrades_to_per_process_limits(self) -> None:
        """Neither lock everyone out of login nor drop the limit entirely."""
        server = fakeredis.FakeServer()
        limiter = RedisRateLimiter(fakeredis.FakeAsyncRedis(server=server))
        server.connected = False

        results = [await limiter.hit("k", limit=2, window_seconds=60) for _ in range(3)]

        assert [r.allowed for r in results] == [True, True, False]
        assert results[-1].retry_after_seconds >= 1
