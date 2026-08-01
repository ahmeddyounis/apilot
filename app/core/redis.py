"""Async Redis clients (lazy singletons): app client + arq job pool."""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from redis.asyncio import Redis

from app.core.config import get_settings

_redis: Redis | None = None
_arq_pool: ArqRedis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        settings = get_settings()
        # Unbounded by default, and rate_limited() is a dependency on nearly every
        # endpoint — so a Redis that hangs rather than refusing (a dropped NAT
        # mapping, a black-holed route, a paused container) blocks every request in
        # the process indefinitely. /health stays green because it does no I/O, which
        # makes it worse: nothing restarts the pod. Bounded, the limiter fails fast
        # and callers get a 503 they can retry.
        _redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_s,
            socket_timeout=settings.redis_socket_timeout_s,
            health_check_interval=30,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None


async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _arq_pool


async def close_arq_pool() -> None:
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.aclose()
    _arq_pool = None
