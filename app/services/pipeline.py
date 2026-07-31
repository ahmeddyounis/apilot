"""The invoice processing pipeline (arq task body).

One task walks an invoice through its stages with status checkpoints; re-running is
safe because each stage begins by checking the current status. Failures are terminal
and honest — the system never guesses.

Remaining stages land in later milestones (P3 validation, P5 screening,
P4 matching + decision gate).
"""

from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.llm.factory import build_llm_client
from app.models import Invoice, InvoiceStatus
from app.services.extraction import run_extraction
from app.services.state_machine import transition
from app.services.validation import run_validation_stage

logger = structlog.get_logger(__name__)

WORKER_ACTOR = "worker"


async def process_invoice_pipeline(invoice_id: UUID) -> None:
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        invoice = await session.scalar(
            select(Invoice).where(Invoice.id == invoice_id).options(selectinload(Invoice.file))
        )
        if invoice is None:
            logger.warning("pipeline_invoice_missing", invoice_id=str(invoice_id))
            return

        structlog.contextvars.bind_contextvars(invoice_id=str(invoice_id))

        if invoice.status is InvoiceStatus.RECEIVED:
            await transition(session, invoice, InvoiceStatus.EXTRACTING, actor=WORKER_ACTOR)
            await session.commit()

        if invoice.status is InvoiceStatus.EXTRACTING:
            llm = build_llm_client(settings, model=settings.extraction_model)
            await run_extraction(session, invoice, llm=llm, settings=settings)
            if invoice.status is not InvoiceStatus.EXTRACTED:
                logger.info("pipeline_terminal", status=invoice.status.value)
                return

        if invoice.status is InvoiceStatus.EXTRACTED:
            passed = await run_validation_stage(session, invoice, settings=settings)
            if not passed:
                logger.info("pipeline_terminal", status=invoice.status.value)
                return

        # Later stages (screening → matching → decision gate) attach here as their
        # milestones land.
        logger.info("pipeline_checkpoint", status=invoice.status.value)
