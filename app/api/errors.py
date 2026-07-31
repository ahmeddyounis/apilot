"""Domain exceptions and the consistent error envelope.

Every non-2xx response has the shape::

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": [...]}}
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


class ApilotError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or []


class NotFoundError(ApilotError):
    status_code = 404
    code = "not_found"


class ConflictError(ApilotError):
    status_code = 409
    code = "conflict"


class InvalidStateError(ApilotError):
    status_code = 409
    code = "invalid_transition"


class UnauthorizedError(ApilotError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(ApilotError):
    status_code = 403
    code = "forbidden"


class RateLimitedError(ApilotError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PayloadTooLargeError(ApilotError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedMediaError(ApilotError):
    status_code = 415
    code = "unsupported_media_type"


_HTTP_CODES = {
    404: "not_found",
    405: "method_not_allowed",
    401: "unauthorized",
    403: "forbidden",
}


def _envelope(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details or [],
        }
    }
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApilotError)
    async def apilot_error_handler(request: Request, exc: ApilotError) -> JSONResponse:
        headers = None
        if isinstance(exc, RateLimitedError):
            headers = {"Retry-After": str(exc.retry_after)}
        return _envelope(request, exc.status_code, exc.code, exc.message, exc.details, headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "loc": [str(part) for part in err.get("loc", [])],
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
            for err in exc.errors()
        ]
        return _envelope(request, 422, "validation_error", "Request validation failed", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        headers = dict(exc.headers) if exc.headers else None
        return _envelope(request, exc.status_code, code, str(exc.detail), headers=headers)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", error=str(exc))
        return _envelope(request, 500, "internal_error", "Internal server error")
