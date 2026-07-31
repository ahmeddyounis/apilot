"""The single choke point for invoice status changes.

Every mutation of ``invoices.status`` goes through :func:`transition`, which
validates the edge, stamps lifecycle timestamps, writes the audit row in the same
transaction, and bumps the Prometheus counter. Nothing else may assign the column.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConcurrentTransitionError, InvalidStateError
from app.core.metrics import INVOICE_TRANSITIONS
from app.models import Invoice, InvoiceStatus
from app.services import audit

ALLOWED: dict[InvoiceStatus, frozenset[InvoiceStatus]] = {
    # EXTRACTION_FAILED from RECEIVED: the queue never picked the invoice up and the
    # rescue cron gave up. Extraction genuinely never happened — say so honestly
    # rather than leaving the invoice pending forever.
    InvoiceStatus.RECEIVED: frozenset({InvoiceStatus.EXTRACTING, InvoiceStatus.EXTRACTION_FAILED}),
    InvoiceStatus.EXTRACTING: frozenset({InvoiceStatus.EXTRACTED, InvoiceStatus.EXTRACTION_FAILED}),
    InvoiceStatus.EXTRACTED: frozenset(
        {InvoiceStatus.MATCHING, InvoiceStatus.EXCEPTION, InvoiceStatus.HELD}
    ),
    InvoiceStatus.MATCHING: frozenset({InvoiceStatus.APPROVED, InvoiceStatus.EXCEPTION}),
    # Review actions: approve/reject; releasing a held/excepted invoice re-runs matching.
    InvoiceStatus.EXCEPTION: frozenset(
        {InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.MATCHING}
    ),
    InvoiceStatus.HELD: frozenset(
        {InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.MATCHING}
    ),
    InvoiceStatus.APPROVED: frozenset({InvoiceStatus.EXPORTED}),
    # Terminal states
    InvoiceStatus.EXTRACTION_FAILED: frozenset(),
    InvoiceStatus.REJECTED: frozenset(),
    InvoiceStatus.EXPORTED: frozenset(),
}

TERMINAL_STATUSES = frozenset(status for status, targets in ALLOWED.items() if not targets)

_DECISION_STATUSES = frozenset(
    {
        InvoiceStatus.APPROVED,
        InvoiceStatus.REJECTED,
        InvoiceStatus.EXCEPTION,
        InvoiceStatus.HELD,
        InvoiceStatus.EXTRACTION_FAILED,
    }
)


def is_transition_allowed(from_status: InvoiceStatus, to_status: InvoiceStatus) -> bool:
    return to_status in ALLOWED.get(from_status, frozenset())


async def transition(
    session: AsyncSession,
    invoice: Invoice,
    to_status: InvoiceStatus,
    *,
    actor: str,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    from_status = invoice.status
    if not is_transition_allowed(from_status, to_status):
        raise InvalidStateError(
            f"Cannot transition invoice {invoice.id} from '{from_status.value}' "
            f"to '{to_status.value}'"
        )

    # Compare-and-set against the row itself. `from_status` is whatever the caller
    # last read, which for the matching stage can be minutes old — the agent runs
    # for a while and never touches the invoice row meanwhile. Without this check
    # the write is blind: the rescue cron can abandon an invoice as
    # `matching -> exception` and the still-running worker then commits
    # `matching -> approved` over the top, leaving an invoice the system publicly
    # gave up on approved for payment, with both outcomes recorded in an
    # append-only audit log that is supposed to be the record of what happened.
    # The lock is taken here rather than at load time so it is held for the
    # decision, not for the whole stage.
    current = await session.scalar(
        select(Invoice.status).where(Invoice.id == invoice.id).with_for_update()
    )
    if current is not None and current != from_status:
        raise ConcurrentTransitionError(
            f"Invoice {invoice.id} moved to '{current.value}' while a transition to "
            f"'{to_status.value}' from '{from_status.value}' was being decided"
        )

    now = datetime.now(UTC)
    invoice.status = to_status

    if to_status is InvoiceStatus.EXTRACTED and invoice.extracted_at is None:
        invoice.extracted_at = now
    if to_status in _DECISION_STATUSES and invoice.decided_at is None:
        invoice.decided_at = now
    if to_status is InvoiceStatus.EXPORTED:
        invoice.exported_at = now
    if reason is not None:
        if to_status is InvoiceStatus.EXCEPTION:
            invoice.exception_reason = reason
        elif to_status is InvoiceStatus.HELD:
            invoice.hold_reason = reason

    audit_payload: dict[str, Any] = dict(payload or {})
    if reason is not None:
        audit_payload["reason"] = reason
    audit.record(
        session,
        entity_type="invoice",
        entity_id=invoice.id,
        invoice_id=invoice.id,
        event_type="state_transition",
        from_status=from_status.value,
        to_status=to_status.value,
        actor=actor,
        payload=audit_payload,
    )

    INVOICE_TRANSITIONS.labels(from_status=from_status.value, to_status=to_status.value).inc()
    await session.flush()
