"""Reject over-large request bodies before anything reads them.

FastAPI resolves the request body *before* it solves dependencies — `await
request.form()` runs ahead of `require_scopes` and `rate_limited` — and Starlette's
multipart parser spools file parts to a temp file with no size cap of its own. So an
anonymous POST of an arbitrarily large body reached disk in full and only then got
its 401, and a rate-limited key's rejected upload was written out before the 429.
Either one is a disk-filling loop that costs the attacker nothing.

This is a *claim* check, not the real limit: Content-Length is client-supplied and a
lying client simply omits it or understates it. That is fine, because the
authoritative enforcement already exists downstream — `storage.store_upload` hashes
and counts the stream and raises the moment the true size passes the cap, without
ever trusting the header. This middleware exists only to make the honest-but-oversize
case cheap and the dishonest case no worse than before.
"""

import structlog
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger(__name__)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            logger.info(
                "request_body_too_large",
                declared_bytes=int(declared),
                limit_bytes=self.max_bytes,
                path=scope.get("path"),
            )
            response = JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": f"Request body exceeds the {self.max_bytes}-byte limit",
                        "details": [],
                    }
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
