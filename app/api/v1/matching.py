from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.api.errors import NotFoundError
from app.core.db import get_session
from app.models import Invoice, MatchingResult
from app.schemas.matching import MatchingResultOut

router = APIRouter(tags=["matching"])


@router.get("/invoices/{invoice_id}/matching", response_model=MatchingResultOut)
async def get_matching_result(
    invoice_id: UUID,
    _auth: AuthContext = Depends(require_scopes("read")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> MatchingResultOut:
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFoundError(f"Invoice {invoice_id} not found")

    result = await session.scalar(
        select(MatchingResult)
        .where(MatchingResult.invoice_id == invoice_id)
        .order_by(MatchingResult.created_at.desc())
        .limit(1)
    )
    if result is None:
        raise NotFoundError(f"Invoice {invoice_id} has no matching run yet")
    return MatchingResultOut(
        invoice_id=str(invoice_id),
        created_at=result.created_at.isoformat(),
        verdict=result.verdict,
        trace=result.trace,
    )
