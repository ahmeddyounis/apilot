"""The invoice processing pipeline (arq task body).

One task walks an invoice through its stages with status checkpoints; re-running is
safe because each stage begins by checking the current status. Failures are terminal
and honest — the system never guesses.

Stage implementations land in later milestones (P2 extraction, P3 validation,
P5 screening, P4 matching); until then a missing stage fails the invoice honestly.
"""

from uuid import UUID

import structlog

from app.core.db import get_sessionmaker
from app.models import Invoice, InvoiceStatus
from app.services.state_machine import transition

logger = structlog.get_logger(__name__)

WORKER_ACTOR = "worker"


class StageNotImplemented(Exception):
    """Raised by stage stubs until their milestone lands."""


async def _stage_extract(invoice: Invoice) -> None:
    raise StageNotImplemented("extraction engine not yet implemented")


async def process_invoice_pipeline(invoice_id: UUID) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        invoice = await session.get(Invoice, invoice_id)
        if invoice is None:
            logger.warning("pipeline_invoice_missing", invoice_id=str(invoice_id))
            return

        structlog.contextvars.bind_contextvars(invoice_id=str(invoice_id))

        if invoice.status is InvoiceStatus.RECEIVED:
            await transition(session, invoice, InvoiceStatus.EXTRACTING, actor=WORKER_ACTOR)
            await session.commit()

        if invoice.status is InvoiceStatus.EXTRACTING:
            try:
                await _stage_extract(invoice)
            except StageNotImplemented as exc:
                invoice.error_detail = str(exc)
                await transition(
                    session,
                    invoice,
                    InvoiceStatus.EXTRACTION_FAILED,
                    actor=WORKER_ACTOR,
                    reason="stage_not_implemented",
                )
                await session.commit()
                logger.info("pipeline_terminal", status=invoice.status.value)
                return

        # Later stages (validation → screening → matching → decision gate) attach here
        # as their milestones land.
