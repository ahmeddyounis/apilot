"""Authentication, authorization, and rate-limiting dependencies."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ForbiddenError, UnauthorizedError
from app.core.db import get_session
from app.core.ratelimit import RateLimiter
from app.core.redis import get_redis
from app.core.security import hash_api_key
from app.models import ApiKey

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
        await limiter.hit(key_id=auth.key_id, bucket=bucket, limit_per_min=limit)

    return dependency
