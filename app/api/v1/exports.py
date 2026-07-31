from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.core.db import get_session
from app.services.exports import build_export, mark_invoices_exported

router = APIRouter(prefix="/exports", tags=["exports"])


@router.get("")
async def export_approved_invoices(
    format: Literal["csv", "quickbooks"] = Query(default="csv"),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    include_exported: bool = Query(default=False),
    mark_exported: bool = Query(
        default=False,
        description="Opt-in state change: transitions included approved invoices to "
        "'exported' after building the file.",
    ),
    auth: AuthContext = Depends(require_scopes("export")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await build_export(
        session,
        export_format=format,
        since=since,
        until=until,
        include_exported=include_exported,
    )
    if mark_exported:
        await mark_invoices_exported(session, result.invoice_ids, auth=auth)
    return Response(
        content=result.content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{result.filename}"'},
    )
