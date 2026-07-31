from typing import Any

import pytest

from app.api.errors import RateLimitedError
from app.core.ratelimit import RateLimiter

pytestmark = pytest.mark.unit


class StubPipeline:
    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._ops: list[tuple[str, str]] = []

    def incr(self, key: str) -> "StubPipeline":
        self._ops.append(("incr", key))
        return self

    def expire(self, key: str, ttl: int) -> "StubPipeline":
        self._ops.append(("expire", key))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, key in self._ops:
            if op == "incr":
                self._store[key] = self._store.get(key, 0) + 1
                results.append(self._store[key])
            else:
                results.append(True)
        return results


class StubRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def pipeline(self) -> StubPipeline:
        return StubPipeline(self.store)


async def test_allows_up_to_limit_then_rejects() -> None:
    limiter = RateLimiter(StubRedis())
    for _ in range(3):
        await limiter.hit(key_id="k1", bucket="default", limit_per_min=3, now=90.0)
    with pytest.raises(RateLimitedError) as exc_info:
        await limiter.hit(key_id="k1", bucket="default", limit_per_min=3, now=95.0)
    assert 1 <= exc_info.value.retry_after <= 60


async def test_new_window_resets_the_count() -> None:
    limiter = RateLimiter(StubRedis())
    await limiter.hit(key_id="k1", bucket="default", limit_per_min=1, now=59.0)
    # Next minute → new key → allowed again.
    await limiter.hit(key_id="k1", bucket="default", limit_per_min=1, now=61.0)


async def test_buckets_and_keys_are_isolated() -> None:
    redis = StubRedis()
    limiter = RateLimiter(redis)
    await limiter.hit(key_id="k1", bucket="ingest", limit_per_min=1, now=10.0)
    await limiter.hit(key_id="k1", bucket="default", limit_per_min=1, now=10.0)
    await limiter.hit(key_id="k2", bucket="ingest", limit_per_min=1, now=10.0)
    assert len(redis.store) == 3
