"""structlog configuration and the request-context ASGI middleware.

Every log line carries the bound ``request_id`` (and, in the worker, ``invoice_id`` /
``stage``) via structlog contextvars. Responses echo ``X-Request-ID`` so API clients
can correlate with server logs and audit rows.
"""

import logging
import re
import sys
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings

REQUEST_ID_HEADER = "X-Request-ID"


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stdout)

    renderer: structlog.typing.Processor
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# W3C traceparent: version-traceid-spanid-flags.
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def _request_id(headers: Headers) -> str:
    """An explicit header wins; otherwise adopt an upstream trace id if one exists.

    This project deliberately does not run OpenTelemetry (see docs/observability.md),
    but if something upstream is already tracing — a mesh sidecar, an API gateway —
    adopting its trace id means the two systems' identifiers line up without taking
    on the SDK.
    """
    explicit = headers.get(REQUEST_ID_HEADER)
    if explicit:
        return explicit
    traceparent = headers.get("traceparent")
    if traceparent and (match := _TRACEPARENT.match(traceparent)):
        return match.group(1)
    return uuid4().hex


def current_request_id() -> str | None:
    """The request id bound by RequestContextMiddleware, or by the worker at job start.

    Reading it from contextvars rather than threading it through every signature is
    why `audit.record` can default it and retro-fit every existing call site at once.
    """
    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None


class RequestContextMiddleware:
    """Bind a request ID to structlog contextvars and echo it on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = _request_id(headers)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, path=scope.get("path"))
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        await self.app(scope, receive, send_with_request_id)
