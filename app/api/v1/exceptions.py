"""The human review queue: invoices needing attention, riskiest money first."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, Numeric, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
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
    offset: int = Query(default=0, ge=0),
    _auth: AuthContext = Depends(require_scopes("read")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> ExceptionsPageOut:
    statuses = (status,) if status in _QUEUE_STATUSES else _QUEUE_STATUSES
    conditions: list[ColumnElement[bool]] = [Invoice.status.in_(statuses)]
    if vendor_id is not None:
        conditions.append(Invoice.vendor_id == vendor_id)
    if min_total is not None:
        conditions.append(Invoice.total >= min_total)

    total_count = (
        await session.scalar(select(func.count()).select_from(Invoice).where(*conditions))
    ) or 0

    # Risk-weighted money first: unknown risk counts as 0.5, unknown total as 0.
    risk = func.coalesce(Invoice.risk_score, cast(Decimal("0.5"), Numeric(4, 3)))
    money = func.coalesce(Invoice.total, cast(Decimal(0), Numeric(14, 2)))
    priority = case((money > 0, risk * money), else_=risk)

    invoices = (
        await session.scalars(
            select(Invoice)
            .where(*conditions)
            .order_by(priority.desc(), Invoice.received_at.asc())
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
