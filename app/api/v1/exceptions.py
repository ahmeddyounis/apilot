"""The human review queue: invoices needing attention, riskiest money first."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.api.errors import ApilotError
from app.core.db import get_session
from app.models import Invoice, InvoiceStatus
from app.schemas.exceptions import ExceptionItemOut, ExceptionsPageOut

router = APIRouter(prefix="/exceptions", tags=["review"])

_QUEUE_STATUSES = (InvoiceStatus.EXCEPTION, InvoiceStatus.HELD)


def _failed_rules(validation_results: dict[str, Any] | None) -> list[str]:
    if not validation_results:
        return []
    return sorted(
        {
            finding["rule_id"]
            for finding in validation_results.get("findings", [])
            if finding.get("severity") == "hard_fail"
        }
    )


def _fraud_codes(fraud_flags: list[dict[str, Any]] | None) -> list[str]:
    return sorted({flag.get("code", "?") for flag in fraud_flags or []})


@router.get("", response_model=ExceptionsPageOut)
async def list_exceptions(
    status: InvoiceStatus | None = Query(default=None),
    vendor_id: UUID | None = Query(default=None),
    min_total: Decimal | None = Query(default=None, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    # Deep pagination re-sorts the whole queue to throw the result away. A review
    # queue this long is a staffing problem, not a paging one.
    offset: int = Query(default=0, ge=0, le=10_000),
    _auth: AuthContext = Depends(require_scopes("read")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> ExceptionsPageOut:
    if status is not None and status not in _QUEUE_STATUSES:
        # Silently widening to the whole queue answered a different question than
        # the one asked: `?status=approved` returned every exception and held
        # invoice, which reads as "these are approved".
        queue = ", ".join(sorted(s.value for s in _QUEUE_STATUSES))
        raise ApilotError(
            f"'{status.value}' is not a review-queue status. This endpoint lists {queue}.",
            code="invalid_filter",
            status_code=422,
        )
    statuses = (status,) if status is not None else _QUEUE_STATUSES
    conditions: list[ColumnElement[bool]] = [Invoice.status.in_(statuses)]
    if vendor_id is not None:
        conditions.append(Invoice.vendor_id == vendor_id)
    if min_total is not None:
        conditions.append(Invoice.total >= min_total)

    total_count = (
        await session.scalar(select(func.count()).select_from(Invoice).where(*conditions))
    ) or 0

    # Risk-weighted money first. The expression itself hasn't changed — unknown risk
    # counts as 0.5, unknown total as 0 — but Postgres now maintains it as a stored
    # generated column (migration 0009) with a partial index over exactly the two
    # statuses this queue shows. Computed inline it could not be indexed at all, so
    # every page recomputed it for every queued invoice and top-N sorted the result.
    invoices = (
        await session.scalars(
            select(Invoice)
            .where(*conditions)
            .order_by(Invoice.review_priority.desc(), Invoice.received_at.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = [
        ExceptionItemOut(
            id=invoice.id,
            status=invoice.status,
            vendor_name=invoice.vendor_name,
            invoice_number=invoice.invoice_number,
            currency=invoice.currency,
            total=invoice.total,
            risk_score=invoice.risk_score,
            exception_reason=invoice.exception_reason,
            hold_reason=invoice.hold_reason,
            failed_rules=_failed_rules(invoice.validation_results),
            fraud_flag_codes=_fraud_codes(invoice.fraud_flags),
            received_at=invoice.received_at,
            decided_at=invoice.decided_at,
        )
        for invoice in invoices
    ]
    return ExceptionsPageOut(items=items, total_count=total_count, limit=limit, offset=offset)
