"""arq worker: pipeline task + housekeeping crons."""

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from uuid import UUID

import structlog
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from app.core.config import get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.core.redis import close_arq_pool, close_redis, get_arq_pool
from app.models import IdempotencyKey, Invoice, InvoiceStatus
from app.services.pipeline import process_invoice_pipeline
from app.services.state_machine import transition

logger = structlog.get_logger(__name__)

# RECEIVED is included deliberately: if Redis was unavailable when the upload was
# accepted, the job was never enqueued and nothing else would ever pick the invoice
# up. Without it, a momentary queue outage silently orphans accepted invoices.
_RESUMABLE_STATUSES = (
    InvoiceStatus.RECEIVED,
    InvoiceStatus.EXTRACTING,
    InvoiceStatus.MATCHING,
)

_TERMINAL_FOR_STATUS = {
    InvoiceStatus.RECEIVED: InvoiceStatus.EXTRACTION_FAILED,
    InvoiceStatus.EXTRACTING: InvoiceStatus.EXTRACTION_FAILED,
    InvoiceStatus.MATCHING: InvoiceStatus.EXCEPTION,
}


async def process_invoice(ctx: dict[str, Any], invoice_id: str) -> None:
    await process_invoice_pipeline(UUID(invoice_id))


async def requeue_stale(ctx: dict[str, Any]) -> None:
    """Rescue invoices whose worker died mid-stage; give up honestly after max tries."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.stale_job_requeue_minutes)
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        stale = (
            await session.scalars(
                select(Invoice).where(
                    Invoice.status.in_(_RESUMABLE_STATUSES),
                    Invoice.updated_at < cutoff,
                )
            )
        ).all()

        pool = await get_arq_pool()
        for invoice in stale:
            if invoice.extraction_attempts >= settings.pipeline_max_tries:
                invoice.error_detail = (
                    f"Abandoned after {invoice.extraction_attempts} pipeline attempts"
                )
                terminal = _TERMINAL_FOR_STATUS[invoice.status]
                await transition(
                    session, invoice, terminal, actor="worker", reason="pipeline_error"
                )
                logger.warning(
                    "stale_invoice_terminal",
                    invoice_id=str(invoice.id),
                    status=terminal.value,
                )
                continue

            invoice.extraction_attempts += 1
            await pool.enqueue_job(
                "process_invoice", str(invoice.id), _job_id=f"pipeline:{invoice.id}"
            )
            logger.info(
                "stale_invoice_requeued",
                invoice_id=str(invoice.id),
                attempt=invoice.extraction_attempts,
            )
        await session.commit()


async def purge_idempotency(ctx: dict[str, Any]) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(IdempotencyKey).where(IdempotencyKey.expires_at < datetime.now(UTC))
            ),
        )
        await session.commit()
        if result.rowcount:
            logger.info("idempotency_purged", rows=result.rowcount)


async def on_startup(ctx: dict[str, Any]) -> None:
    configure_logging(get_settings())
    logger.info("worker_started")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    await close_arq_pool()
    await close_redis()
    logger.info("worker_stopped")


class WorkerSettings:
    functions: ClassVar = [process_invoice]
    cron_jobs: ClassVar = [
        cron(requeue_stale, minute=set(range(0, 60, 5)), run_at_startup=False),
        cron(purge_idempotency, minute={7}),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    job_timeout = 600
    max_tries = get_settings().pipeline_max_tries
    # We never read results; keeping them would also block same-id re-enqueues.
    keep_result = 0
