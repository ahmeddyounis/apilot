"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.v1.analytics import router as analytics_router
from app.api.v1.exceptions import router as exceptions_router
from app.api.v1.exports import router as exports_router
from app.api.v1.health import router as health_router
from app.api.v1.invoices import router as invoices_router
from app.api.v1.matching import router as matching_router
from app.api.v1.purchase_orders import router as purchase_orders_router
from app.api.v1.receipts import router as receipts_router
from app.api.v1.review import router as review_router
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.metrics import setup_metrics
from app.core.redis import close_arq_pool, close_redis

logger = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        # Each shutdown step runs even if an earlier one fails. Unguarded, a raising
        # dispose_engine() would leave the Redis connections open, and a pod that
        # can't release its resources cleanly on SIGTERM leaks them across every
        # rollout.
        for name, close in (
            ("database", dispose_engine),
            ("arq_pool", close_arq_pool),
            ("redis", close_redis),
        ):
            try:
                await close()
            except Exception:
                logger.exception("shutdown_step_failed", component=name)

    # Interactive docs are a development affordance. In prod they publish the full
    # request/response schema of a payments system to anyone who can reach the port,
    # and there is no auth in front of them (`/docs`, `/redoc`, `/openapi.json` are
    # all unauthenticated by design so the demo works).
    public_docs = settings.env != "prod"
    app = FastAPI(
        title="APilot — AI Invoice Intelligence API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if public_docs else None,
        redoc_url="/redoc" if public_docs else None,
        openapi_url="/openapi.json" if public_docs else None,
    )
    app.state.settings = settings

    # Outermost: the size check must run before anything reads the body, and the
    # request-id binding must wrap whatever it rejects.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes)
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    setup_metrics(app)

    app.include_router(health_router)

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(invoices_router)
    api_v1.include_router(matching_router)
    api_v1.include_router(review_router)
    api_v1.include_router(exceptions_router)
    api_v1.include_router(purchase_orders_router)
    api_v1.include_router(receipts_router)
    api_v1.include_router(exports_router)
    api_v1.include_router(analytics_router)
    app.include_router(api_v1)

    return app


app = create_app()
