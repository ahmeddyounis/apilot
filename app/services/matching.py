"""Matching stage: run the LangGraph workflow, persist verdict + trace, gate."""

from decimal import Decimal
from uuid import uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import Matcher
from app.agent.state import InvoiceLineData, MatchingState
from app.core.config import Settings
from app.llm.client import LLMClient
from app.models import Invoice, InvoiceStatus, MatchingResult
from app.services import audit
from app.services.state_machine import transition

logger = structlog.get_logger(__name__)


def _initial_state(invoice: Invoice, trace_id: str) -> MatchingState:
    flags = invoice.fraud_flags or []
    fraud_codes = sorted({flag.get("code", "?") for flag in flags})
    # Low-severity flags annotate the verdict but never block auto-approval —
    # otherwise F-100 (first invoice from a vendor) would freeze a fresh system.
    blocking = sorted(
        {flag.get("code", "?") for flag in flags if flag.get("severity") in ("high", "medium")}
    )
    return MatchingState(
        invoice_id=invoice.id,
        trace_id=trace_id,
        currency=invoice.currency or "USD",
        total=invoice.total if invoice.total is not None else Decimal(0),
        vendor_id=invoice.vendor_id,
        vendor_name=invoice.vendor_name,
        invoice_number=invoice.invoice_number,
        po_number_raw=invoice.po_number,
        invoice_date=invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        fraud_flag_codes=fraud_codes,
        blocking_fraud_codes=blocking,
        invoice_lines=[
            InvoiceLineData(
                index=index,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
            )
            for index, line in enumerate(invoice.line_items)
        ],
    )


def _exception_reason(state: MatchingState) -> str:
    verdict = state.verdict
    if verdict is None:
        return "matching:no_verdict"
    if any(d.type == "missing_po" for d in verdict.discrepancies):
        return "missing_po"
    if verdict.discrepancies:
        return f"matching:{verdict.discrepancies[0].type}"
    if state.total > 0 and not verdict.auto_approve_eligible and verdict.checks.all_pass():
        return "over_auto_approve_threshold"
    return "matching:checks_failed"


async def run_matching_stage(
    session: AsyncSession,
    invoice: Invoice,
    *,
    llm: LLMClient,
    settings: Settings,
) -> None:
    """Invoice must be in status MATCHING. Transitions to APPROVED or EXCEPTION,
    persists the verdict + replayable trace, and commits."""
    await session.refresh(invoice, attribute_names=["line_items"])
    # Close the read transaction before the agent runs. Everything below re-reads what
    # it needs, and holding this one open left a Postgres backend `idle in
    # transaction` for the length of up to four LLM turns — blocking vacuum on the
    # table the whole system writes to, multiplied by the worker's concurrency.
    await session.commit()
    trace_id = uuid4().hex

    matcher = Matcher(session=session, llm=llm, settings=settings)
    final_state = await matcher.run(_initial_state(invoice, trace_id))
    verdict = final_state.verdict
    if verdict is None:  # pragma: no cover — assemble_verdict always runs
        raise RuntimeError("matching graph ended without a verdict")

    result = MatchingResult(
        invoice_id=invoice.id,
        po_id=final_state.po.id if final_state.po else None,
        trace_id=trace_id,
        verdict=verdict.model_dump(),
        trace=[event.model_dump() for event in final_state.events],
    )
    session.add(result)

    for event in final_state.events:
        audit.record(
            session,
            entity_type="invoice",
            entity_id=invoice.id,
            invoice_id=invoice.id,
            event_type="agent_step",
            actor="agent",
            payload={
                "trace_id": trace_id,
                "seq": event.seq,
                "node": event.node,
                "duration_ms": event.duration_ms,
                "summary": event.summary,
            },
        )
    audit.record(
        session,
        entity_type="invoice",
        entity_id=invoice.id,
        invoice_id=invoice.id,
        event_type="matching_completed",
        actor="agent",
        payload={
            "trace_id": trace_id,
            "verdict": verdict.verdict,
            "auto_approve_eligible": verdict.auto_approve_eligible,
            "discrepancy_types": [d.type for d in verdict.discrepancies],
            "investigated": final_state.investigated,
        },
    )

    if verdict.verdict == "auto_approve":
        await transition(
            session,
            invoice,
            InvoiceStatus.APPROVED,
            actor="agent",
            payload={"trace_id": trace_id},
        )
    else:
        await transition(
            session,
            invoice,
            InvoiceStatus.EXCEPTION,
            actor="agent",
            reason=_exception_reason(final_state),
            payload={"trace_id": trace_id},
        )
    await session.commit()
    logger.info(
        "matching_completed",
        verdict=verdict.verdict,
        discrepancies=len(verdict.discrepancies),
        investigated=final_state.investigated,
    )
