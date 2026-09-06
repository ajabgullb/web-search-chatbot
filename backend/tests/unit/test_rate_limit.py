"""In-process rate limiter."""

from __future__ import annotations

from app.core.rate_limit import InMemoryRateLimiter


def test_allows_up_to_the_limit_then_blocks() -> None:
    limiter = InMemoryRateLimiter()

    allowed = [limiter.hit("k", limit=3, window_seconds=60).allowed for _ in range(3)]
    blocked = limiter.hit("k", limit=3, window_seconds=60)

    assert allowed == [True, True, True]
    assert blocked.allowed is False
    assert blocked.retry_after_seconds > 0


def test_buckets_are_independent() -> None:
    limiter = InMemoryRateLimiter()

    for _ in range(3):
        limiter.hit("a", limit=3, window_seconds=60)

    assert limiter.hit("a", limit=3, window_seconds=60).allowed is False
    assert limiter.hit("b", limit=3, window_seconds=60).allowed is True


def test_entries_leave_the_window() -> None:
    """A zero-length window means nothing is ever still inside it."""
    limiter = InMemoryRateLimiter()

    for _ in range(10):
        assert limiter.hit("k", limit=1, window_seconds=0).allowed is True
