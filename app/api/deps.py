"""Authentication, authorization, and rate-limiting dependencies."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader
from redis.exceptions import RedisError
from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    ForbiddenError,
    RateLimitedError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.core.db import get_session
from app.core.ratelimit import RateLimiter
from app.core.redis import get_redis
from app.core.security import hash_api_key
from app.models import ApiKey

logger = structlog.get_logger(__name__)

API_KEY_HEADER = "X-API-Key"

api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False, scheme_name="ApiKeyAuth")

_LAST_USED_CUTOFF = text("now() - interval '5 minutes'")


@dataclass(frozen=True)
class AuthContext:
    key_id: UUID
    name: str
    scopes: frozenset[str]

    @property
    def actor(self) -> str:
        return f"api_key:{self.key_id}"


async def authenticate(
    raw_key: str | None = Security(api_key_scheme),
    session: AsyncSession = Depends(get_session),
) -> AuthContext:
    if not raw_key:
        raise UnauthorizedError(f"Missing {API_KEY_HEADER} header")

    key_hash = hash_api_key(raw_key)
    api_key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
    if api_key is None or api_key.revoked_at is not None:
        raise UnauthorizedError("Invalid or revoked API key")
    if api_key.expires_at is not None and api_key.expires_at <= datetime.now(UTC):
        # Distinguished from revocation on purpose: an expired key is a housekeeping
        # problem its owner can fix, a revoked one is a decision someone made.
        raise UnauthorizedError("API key expired")

    # Throttled usage stamp: at most one hot-row write per key per 5 minutes.
    await session.execute(
        update(ApiKey)
        .where(
            ApiKey.id == api_key.id,
            or_(
                ApiKey.last_used_at.is_(None),
                ApiKey.last_used_at < _LAST_USED_CUTOFF,
            ),
        )
        .values(last_used_at=text("now()"))
    )
    await session.commit()

    structlog.contextvars.bind_contextvars(api_key_id=str(api_key.id))
    return AuthContext(key_id=api_key.id, name=api_key.name, scopes=frozenset(api_key.scopes))


AuthDependency = Callable[..., Coroutine[Any, Any, AuthContext]]


def require_scopes(*required: str) -> AuthDependency:
    async def dependency(auth: AuthContext = Depends(authenticate)) -> AuthContext:
        missing = set(required) - auth.scopes
        if missing:
            raise ForbiddenError(f"API key lacks required scope(s): {', '.join(sorted(missing))}")
        return auth

    return dependency


def rate_limited(bucket: str = "default") -> Callable[..., Coroutine[Any, Any, None]]:
    async def dependency(request: Request, auth: AuthContext = Depends(authenticate)) -> None:
        settings = request.app.state.settings
        if not settings.rate_limit_enabled:
            return
        limit = (
            settings.rate_limit_ingest_per_min
            if bucket == "ingest"
            else settings.rate_limit_default_per_min
        )
        limiter = RateLimiter(get_redis())
        try:
            await limiter.hit(key_id=auth.key_id, bucket=bucket, limit_per_min=limit)
        except RateLimitedError:
            raise
        except (RedisError, OSError, TimeoutError) as exc:
            # Fail closed, and quickly. This dependency runs on nearly every endpoint,
            # so a Redis that is unreachable — or, worse, hanging — would otherwise
            # decide whether the whole API responds. Answering 503 lets the caller
            # retry and lets a load balancer see the pod as unhealthy; the previous
            # behaviour was to wait forever while /health stayed green.
            # OSError/TimeoutError as well as RedisError: a socket-level failure
            # does not always reach us wrapped in the client's own hierarchy.
            logger.warning("rate_limiter_unavailable", error=str(exc)[:200])
            raise ServiceUnavailableError("Rate limiter unavailable; retry shortly") from exc

    return dependency
