"""Accounting exports.

Two verbs, because they do different things. ``GET`` is a preview: build the file
and change nothing, so it is safe to retry, cache, or open in a browser. ``POST`` is
a checkout: build the same file and transition the invoices in it to ``exported`` in
the same request, so the batch is marked delivered only if it was actually handed
over. A ``GET`` that mutates on a query parameter is reachable by a link crawler, a
browser prefetch, or a CDN warming a URL.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.core.db import get_session
from app.services.exports import ExportResult, build_export, mark_invoices_exported

router = APIRouter(prefix="/exports", tags=["exports"])


def _as_file(result: ExportResult) -> Response:
    return Response(
        content=result.content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{result.filename}"'},
    )


@router.get("")
async def preview_export(
    format: Literal["csv", "quickbooks"] = Query(default="csv"),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    include_exported: bool = Query(default=False),
    _auth: AuthContext = Depends(require_scopes("export")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Build the accounting file without changing any invoice's status."""
    return _as_file(
        await build_export(
            session,
            export_format=format,
            since=since,
            until=until,
            include_exported=include_exported,
        )
    )


@router.post("")
async def checkout_export(
    format: Literal["csv", "quickbooks"] = Query(default="csv"),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    include_exported: bool = Query(default=False),
    auth: AuthContext = Depends(require_scopes("export")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Build the accounting file and mark the invoices in it as exported."""
    result = await build_export(
        session,
        export_format=format,
        since=since,
        until=until,
        include_exported=include_exported,
    )
    await mark_invoices_exported(session, result.invoice_ids, auth=auth)
    return _as_file(result)
