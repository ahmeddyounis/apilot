"""Liveness and readiness endpoints (unauthenticated)."""

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.core.db import get_engine
from app.core.redis import get_redis

router = APIRouter(tags=["health"])

_CHECK_TIMEOUT_S = 2.0


@router.get("/health")
async def health() -> dict[str, str]:
    """Pure liveness — no I/O, so an infra blip never restarts containers."""
    return {"status": "ok", "version": __version__}


async def _check_database() -> str:
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_S):
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # readiness reports failures, it never raises
        return f"error: {type(exc).__name__}"


async def _check_redis() -> str:
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_S):
            await get_redis().ping()
        return "ok"
    except Exception as exc:  # readiness reports failures, it never raises
        return f"error: {type(exc).__name__}"


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    database, redis = await asyncio.gather(_check_database(), _check_redis())
    components: dict[str, Any] = {"database": database, "redis": redis}
    healthy = all(status == "ok" for status in components.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "components": components},
    )
