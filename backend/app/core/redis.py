"""The shared Redis client.

Built once in the lifespan and shared by everything that coordinates across
replicas. Optional outside production: with no ``REDIS_URL`` the application
falls back to in-process equivalents.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings


def build_redis_client(settings: Settings) -> Redis | None:
    if not settings.redis_url:
        return None
    # from_url does not connect; the first command does. A Redis that is down
    # at startup therefore shows up in readiness, not as a crash loop.
    return Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
        health_check_interval=30,
    )


async def ping(client: Redis) -> bool:
    try:
        return bool(await client.ping())
    except RedisError:
        return False
