"""Rate limiting.

Phase 2 ships a single in-process limiter, which is correct for one API
process and enough to blunt credential stuffing and reset-email spam. It sits
behind a Protocol because Phase 8 swaps in a Redis-backed limiter that works
across replicas; the call sites will not change.

The in-process limiter is deliberately honest about its limitation: with N
replicas the effective limit is N x the configured value.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class RateLimiter(Protocol):
    def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        """Record one attempt against ``key`` and say whether it is allowed."""
        ...


class InMemoryRateLimiter:
    """Sliding-window counter kept in process memory.

    A deque of timestamps per key: entries older than the window are dropped
    on each hit, so memory stays proportional to recent traffic rather than
    to total distinct keys over time.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        now = time.monotonic()
        cutoff = now - window_seconds
        bucket = self._hits[key]

        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
            return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)

        bucket.append(now)
        if not bucket:
            del self._hits[key]
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    def reset(self) -> None:
        """Clear all counters. Used by tests, never in request handling."""
        self._hits.clear()
