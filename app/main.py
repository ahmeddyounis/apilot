"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.v1.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.redis import close_redis


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await dispose_engine()
        await close_redis()

    app = FastAPI(
        title="APilot — AI Invoice Intelligence API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    app.include_router(health_router)

    return app


app = create_app()
