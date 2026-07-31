"""Redis fixed-window rate limiter (one INCR+EXPIRE pipeline round-trip per hit)."""

import time
from typing import Any, Protocol
from uuid import UUID

from app.api.errors import RateLimitedError

WINDOW_SECONDS = 60
# Keys outlive the window slightly so clock skew never resurrects a stale bucket.
KEY_TTL_SECONDS = 90


class SupportsPipeline(Protocol):
    def pipeline(self) -> Any: ...


class RateLimiter:
    def __init__(self, redis: SupportsPipeline) -> None:
        self._redis = redis

    async def hit(
        self,
        *,
        key_id: UUID | str,
        bucket: str,
        limit_per_min: int,
        now: float | None = None,
    ) -> None:
        """Record one request; raise RateLimitedError beyond the per-minute limit."""
        ts = time.time() if now is None else now
        window = int(ts // WINDOW_SECONDS)
        redis_key = f"rl:{key_id}:{bucket}:{window}"

        pipe = self._redis.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, KEY_TTL_SECONDS)
        results = await pipe.execute()
        count = int(results[0])

        if count > limit_per_min:
            retry_after = max(1, WINDOW_SECONDS - int(ts % WINDOW_SECONDS))
            raise RateLimitedError(
                f"Rate limit exceeded for bucket '{bucket}'", retry_after=retry_after
            )
