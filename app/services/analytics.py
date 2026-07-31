"""Operational analytics: the numbers the README's business case is written in."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, InvoiceStatus, LlmCall, ReviewAction

_DECIDED = (
    InvoiceStatus.APPROVED,
    InvoiceStatus.EXPORTED,
    InvoiceStatus.EXCEPTION,
    InvoiceStatus.HELD,
    InvoiceStatus.REJECTED,
    InvoiceStatus.EXTRACTION_FAILED,
)


async def summary(session: AsyncSession, *, since: datetime | None) -> dict[str, Any]:
    conditions: list[ColumnElement[bool]] = []
    if since is not None:
        conditions.append(Invoice.received_at >= since)

    status_rows = (
        await session.execute(
            select(Invoice.status, func.count()).where(*conditions).group_by(Invoice.status)
        )
    ).all()
    by_status = {status.value: count for status, count in status_rows}
    total_invoices = sum(by_status.values())

    # Auto-approval = approved/exported with zero human review actions.
    reviewed = select(distinct(ReviewAction.invoice_id)).scalar_subquery()
    auto_approved = (
        await session.scalar(
            select(func.count()).where(
                *conditions,
                Invoice.status.in_((InvoiceStatus.APPROVED, InvoiceStatus.EXPORTED)),
                Invoice.id.not_in(reviewed),
            )
        )
    ) or 0
    decided = (
        await session.scalar(select(func.count()).where(*conditions, Invoice.status.in_(_DECIDED)))
    ) or 0

    reason_rows = (
        await session.execute(
            select(Invoice.exception_reason, func.count())
            .where(*conditions, Invoice.status == InvoiceStatus.EXCEPTION)
            .group_by(Invoice.exception_reason)
        )
    ).all()

    cycle_seconds = func.extract("epoch", Invoice.decided_at - Invoice.received_at)
    cycle_row = (
        await session.execute(
            select(
                func.avg(cycle_seconds),
                func.percentile_cont(0.95).within_group(cycle_seconds.asc()),
            ).where(*conditions, Invoice.decided_at.is_not(None))
        )
    ).one()

    llm_conditions: list[ColumnElement[bool]] = []
    if since is not None:
        llm_conditions.append(LlmCall.created_at >= since)
    cost_total = (
        await session.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(*llm_conditions)
        )
    ) or Decimal(0)
    invoices_with_calls = (
        await session.scalar(
            select(func.count(distinct(LlmCall.invoice_id))).where(
                *llm_conditions, LlmCall.invoice_id.is_not(None)
            )
        )
    ) or 0
    by_model_rows = (
        await session.execute(
            select(
                LlmCall.model,
                func.sum(LlmCall.cost_usd),
                func.count(),
                func.sum(case((LlmCall.cache_read_input_tokens > 0, 1), else_=0)),
            )
            .where(*llm_conditions)
            .group_by(LlmCall.model)
        )
    ).all()

    return {
        "invoices": {"total": total_invoices, "by_status": by_status},
        "auto_approval_rate": round(auto_approved / decided, 4) if decided else None,
        "decided": decided,
        "exception_reasons": {reason or "unknown": count for reason, count in reason_rows},
        "cycle_time_seconds": {
            "avg": round(float(cycle_row[0]), 2) if cycle_row[0] is not None else None,
            "p95": round(float(cycle_row[1]), 2) if cycle_row[1] is not None else None,
        },
        "llm_cost": {
            "total_usd": str(cost_total),
            "avg_per_invoice_usd": (
                str((cost_total / invoices_with_calls).quantize(Decimal("0.000001")))
                if invoices_with_calls
                else None
            ),
            "by_model": {
                model: {
                    "cost_usd": str(cost),
                    "calls": calls,
                    "cache_hit_calls": int(cache_hits or 0),
                }
                for model, cost, calls, cache_hits in by_model_rows
            },
        },
    }
