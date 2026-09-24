"""Rate limiting.

Both limiters implement the same sliding-window log: a key may make ``limit``
allowed attempts in any ``window_seconds``-long span. A sliding window rather
than fixed buckets, because a fixed window admits twice the limit in a burst
straddling a bucket boundary — exactly what a credential-stuffing script would
learn to do. Only allowed attempts are recorded, so a blocked client that keeps
hammering does not extend its own ban, and memory per key is bounded by
``limit``.

- :class:`RedisRateLimiter` shares the window across every API replica. It is
  what production runs (``REDIS_URL`` is required there).
- :class:`InMemoryRateLimiter` keeps it in process memory: correct for one
  process, and ``N x limit`` with N replicas.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        """Record one attempt against ``key`` and say whether it is allowed."""
        ...


def _retry_after(seconds: float) -> int:
    # Whole seconds for the Retry-After header, rounded up and never zero: a
    # client told to wait 0 s retries at once and is blocked again.
    return max(1, math.ceil(seconds))


class InMemoryRateLimiter:
    """Sliding-window log kept in process memory.

    A deque of timestamps per key: entries older than the window are dropped
    on each hit, so memory stays proportional to recent traffic rather than
    to total distinct keys over time.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        return self.hit_now(key, limit=limit, window_seconds=window_seconds)

    def hit_now(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        """The synchronous core, also used as the Redis limiter's fallback."""
        now = time.monotonic()
        cutoff = now - window_seconds
        bucket = self._hits[key]

        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            oldest = bucket[0] if bucket else now
            return RateLimitDecision(
                allowed=False, retry_after_seconds=_retry_after(oldest + window_seconds - now)
            )

        bucket.append(now)
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    def reset(self) -> None:
        """Clear all counters. Used by tests, never in request handling."""
        self._hits.clear()


# The whole check-and-record runs as one script, atomically on the server.
# Done as separate commands from Python, two replicas could both count 9 of 10
# and both admit an 11th attempt.
#
# The clock is Redis's own (TIME), not the caller's: replicas whose clocks
# drift would otherwise disagree about which entries are still in the window.
# Each entry needs a unique member (ARGV[3]); two hits in the same millisecond
# with equal members would collapse into one and under-count.
_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)
if redis.call('ZCARD', key) >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if #oldest == 0 then
        return {0, window_ms}
    end
    return {0, tonumber(oldest[2]) + window_ms - now_ms}
end

redis.call('ZADD', key, now_ms, ARGV[3])
-- The key disappears once its newest entry leaves the window, so idle
-- clients cost nothing.
redis.call('PEXPIRE', key, window_ms)
return {1, 0}
"""


class RedisRateLimiter:
    """Sliding-window log in a Redis sorted set, shared by every replica.

    If Redis cannot be reached, it degrades to a per-process limiter rather
    than failing either way. Failing closed would lock every user out of
    logging in during a Redis outage; failing open would drop brute-force
    protection entirely. Per-process limits are weaker (N x the limit with N
    replicas) but still limits, and the outage is logged.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client
        self._script = client.register_script(_SLIDING_WINDOW_SCRIPT)
        self._fallback = InMemoryRateLimiter()

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        try:
            allowed, retry_ms = await self._script(
                keys=[f"rate-limit:{key}"],
                args=[limit, window_seconds * 1000, uuid.uuid4().hex],
            )
        except RedisError:
            # The key names a client (an IP or user id); the bucket is enough.
            logger.warning("rate_limit_degraded", extra={"bucket": key.split(":", 1)[0]})
            return self._fallback.hit_now(key, limit=limit, window_seconds=window_seconds)
        if allowed:
            return RateLimitDecision(allowed=True, retry_after_seconds=0)
        return RateLimitDecision(allowed=False, retry_after_seconds=_retry_after(retry_ms / 1000))


def build_rate_limiter(client: Redis | None) -> RateLimiter:
    if client is None:
        return InMemoryRateLimiter()
    return RedisRateLimiter(client)
