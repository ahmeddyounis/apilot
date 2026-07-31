from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.api.errors import NotFoundError
from app.core.db import get_session
from app.models import Invoice
from app.schemas.review import ReviewIn, ReviewOut
from app.services.review import apply_review

router = APIRouter(tags=["review"])


@router.post("/invoices/{invoice_id}/review", response_model=ReviewOut)
async def review_invoice(
    invoice_id: UUID,
    payload: ReviewIn,
    request: Request,
    auth: AuthContext = Depends(require_scopes("review")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> ReviewOut:
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFoundError(f"Invoice {invoice_id} not found")

    corrections = (
        [(c.field_path, c.new_value) for c in payload.corrections] if payload.corrections else None
    )
    applied, validation_passed = await apply_review(
        session,
        invoice,
        auth=auth,
        action=payload.action,
        note=payload.note,
        corrections=corrections,
        rerun_matching=payload.rerun_matching,
        settings=request.app.state.settings,
    )
    return ReviewOut(
        invoice_id=str(invoice.id),
        action=payload.action,
        status=invoice.status,
        corrections_applied=applied,
        validation_passed=validation_passed,
    )
