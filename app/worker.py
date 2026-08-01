"""arq worker: pipeline task + housekeeping crons."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from uuid import UUID

import structlog
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from app.api.errors import ConcurrentTransitionError
from app.core.config import get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.core.metrics import PIPELINE_ABANDONED, PIPELINE_REQUEUES
from app.core.redis import close_arq_pool, close_redis, get_arq_pool
from app.core.worker_metrics import (
    gauge_refresh_loop,
    start_metrics_server,
    stop_metrics_server,
)
from app.models import AuditLog, IdempotencyKey, Invoice, InvoiceStatus
from app.services import audit
from app.services.pipeline import process_invoice_pipeline
from app.services.state_machine import transition

logger = structlog.get_logger(__name__)

# RECEIVED is included deliberately: if Redis was unavailable when the upload was
# accepted, the job was never enqueued and nothing else would ever pick the invoice
# up. Without it, a momentary queue outage silently orphans accepted invoices.
_RESUMABLE_STATUSES = (
    InvoiceStatus.RECEIVED,
    InvoiceStatus.EXTRACTING,
    # EXTRACTED is a resting state the pipeline sits in across validation and
    # screening — embedding inference plus several queries — and a worker that dies
    # in that window leaves the invoice there for good. Same orphan class as
    # RECEIVED: no retry, no cron, no human queue, and a client holding a 202.
    InvoiceStatus.EXTRACTED,
    InvoiceStatus.MATCHING,
)

_TERMINAL_FOR_STATUS = {
    InvoiceStatus.RECEIVED: InvoiceStatus.EXTRACTION_FAILED,
    InvoiceStatus.EXTRACTING: InvoiceStatus.EXTRACTION_FAILED,
    InvoiceStatus.EXTRACTED: InvoiceStatus.EXCEPTION,
    InvoiceStatus.MATCHING: InvoiceStatus.EXCEPTION,
}


async def process_invoice(
    ctx: dict[str, Any], invoice_id: str, request_id: str | None = None
) -> None:
    # A keyword with a default, deliberately: arq pickles job arguments at enqueue
    # time, so a positional parameter would raise TypeError on every job already
    # sitting on the queue across a deploy — and arq drops a job that raises a plain
    # exception rather than retrying it, so those invoices would be lost until the
    # rescue cron found them fifteen minutes later.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(invoice_id=invoice_id, job_try=ctx.get("job_try"))
    if request_id:
        structlog.contextvars.bind_contextvars(request_id=request_id)
    await process_invoice_pipeline(UUID(invoice_id))


async def requeue_stale(ctx: dict[str, Any]) -> None:
    """Rescue invoices whose worker died mid-stage; give up honestly after max tries."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.stale_job_requeue_minutes)
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        stale = (
            await session.scalars(
                select(Invoice)
                .where(
                    Invoice.status.in_(_RESUMABLE_STATUSES),
                    Invoice.updated_at < cutoff,
                )
                # Bounded: after an outage every stalled invoice is stale at once, and
                # an unbounded sweep would load all of them and try to enqueue the lot
                # in one tick. Oldest first, and the cron runs again in five minutes.
                .order_by(Invoice.updated_at.asc())
                .limit(settings.stale_batch_size)
            )
        ).all()

        pool = await get_arq_pool()
        for invoice in stale:
            # One invoice per transaction: a decision already made must not be undone
            # because a *later* invoice's enqueue failed. A single Redis hiccup used
            # to roll back the whole batch, including terminal transitions and their
            # audit rows.
            try:
                if invoice.pipeline_requeues >= settings.pipeline_max_tries:
                    invoice.error_detail = (
                        f"Abandoned after {invoice.pipeline_requeues} pipeline attempts"
                    )
                    terminal = _TERMINAL_FOR_STATUS[invoice.status]
                    await transition(
                        session, invoice, terminal, actor="worker", reason="pipeline_error"
                    )
                    PIPELINE_ABANDONED.labels(terminal_status=terminal.value).inc()
                    logger.warning(
                        "stale_invoice_terminal",
                        invoice_id=str(invoice.id),
                        status=terminal.value,
                    )
                    await session.commit()
                    continue

                # arq returns None when a job with this id is already queued or
                # running. That is the normal case for a backlogged queue, and
                # counting it as an attempt let the cron exhaust an invoice's budget
                # and kill it while its original job was still waiting to start.
                # Recover the request that started this invoice, so correlation
                # survives the retry path — the one you most want to trace.
                originating = await session.scalar(
                    select(AuditLog.request_id)
                    .where(
                        AuditLog.invoice_id == invoice.id,
                        AuditLog.event_type == "upload",
                    )
                    .order_by(AuditLog.id.asc())
                    .limit(1)
                )
                job = await pool.enqueue_job(
                    "process_invoice",
                    str(invoice.id),
                    request_id=originating,
                    _job_id=f"pipeline:{invoice.id}",
                )
                if job is None:
                    logger.info("stale_invoice_already_queued", invoice_id=str(invoice.id))
                    continue

                invoice.pipeline_requeues += 1
                # The audit log had a hole exactly where 'we noticed this was
                # stuck and tried again' belongs: abandonment wrote a row, rescue
                # only logged.
                audit.record(
                    session,
                    entity_type="invoice",
                    entity_id=invoice.id,
                    invoice_id=invoice.id,
                    event_type="pipeline_requeued",
                    actor="worker",
                    payload={
                        "attempt": invoice.pipeline_requeues,
                        "from_status": invoice.status.value,
                    },
                )
                await session.commit()
                PIPELINE_REQUEUES.inc()
                logger.info(
                    "stale_invoice_requeued",
                    invoice_id=str(invoice.id),
                    requeue=invoice.pipeline_requeues,
                )
            except ConcurrentTransitionError:
                # A live worker finished this invoice between our query and our
                # decision. Its outcome is the real one; leave it alone.
                await session.rollback()
                logger.info("stale_invoice_moved_on", invoice_id=str(invoice.id))
            except Exception:
                await session.rollback()
                logger.exception("stale_invoice_requeue_failed", invoice_id=str(invoice.id))


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
    settings = get_settings()
    configure_logging(settings)
    start_metrics_server(settings)
    ctx["gauge_refresh"] = asyncio.create_task(gauge_refresh_loop(settings))
    logger.info("worker_started")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    # Stop the refresh task before disposing the engine, or its last tick queries a
    # disposed engine and the shutdown log ends in a traceback.
    task: asyncio.Task[None] | None = ctx.pop("gauge_refresh", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    stop_metrics_server()
    for name, close in (
        ("database", dispose_engine),
        ("arq_pool", close_arq_pool),
        ("redis", close_redis),
    ):
        try:
            await close()
        except Exception:
            logger.exception("shutdown_step_failed", component=name)
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
    # arq only re-queues a job for Retry/RetryJob/CancelledError; a plain exception
    # (an LLM error, a dropped connection, a killed pod) marks the job finished and
    # drops it. The pipeline raises plain exceptions, so this bound is never the
    # thing that retries anything — `requeue_stale` above is the real recovery path,
    # and it enforces the same budget via invoices.pipeline_requeues. Kept aligned
    # so an explicit Retry, if one is ever raised, obeys the same limit.
    max_jobs = get_settings().worker_max_jobs
    max_tries = get_settings().pipeline_max_tries
    # We never read results; keeping them would also block same-id re-enqueues.
    keep_result = 0
