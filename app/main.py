"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.v1.health import router as health_router
from app.api.v1.invoices import router as invoices_router
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.metrics import setup_metrics
from app.core.redis import close_arq_pool, close_redis


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await dispose_engine()
        await close_arq_pool()
        await close_redis()

    app = FastAPI(
        title="APilot — AI Invoice Intelligence API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    setup_metrics(app)

    app.include_router(health_router)

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(invoices_router)
    app.include_router(api_v1)

    return app


app = create_app()
