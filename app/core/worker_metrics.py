"""Metrics the worker owns: the exporter, and the gauges only it can measure.

**Why this file exists.** `setup_metrics` is called from `create_app`, so only the
API ever served `/metrics` — while `apilot_invoice_transitions_total` and
`apilot_llm_cost_usd_total` are incremented almost entirely inside the worker, whose
increments landed in an in-process registry nothing scraped. What `/metrics` actually
published was the handful of transitions the API performs itself: review decisions
and export marking. A metric named for the pipeline that describes the review desk is
worse than no metric, because you'd trust it.

**Why a daemon thread and not the event loop.** The worker's loop is not reliably
responsive — Hungarian assignment and, until recently, ONNX inference run on it — so
a loop-bound exporter would miss scrapes exactly when the worker is busiest, and a
scrape timeout reads as "worker down" at the moment the numbers matter most.
`prometheus_client` guards every value with a lock, so a WSGI thread reading the
registry while the loop writes to it is safe.

**Why not multiprocess mode or a pushgateway.** Multiprocess mode aggregates several
processes behind *one* scrape target; the API and worker are separate deployments with
separate addresses, and there is no shared filesystem to point it at. A pushgateway
makes series sticky — a dead worker keeps publishing its last values, and
`up{job="apilot-worker"}` stops meaning the worker is alive, which is the signal you
most want when the queue backs up.
"""

import asyncio
import time
from wsgiref.simple_server import WSGIServer

import structlog
from prometheus_client import Gauge, start_http_server
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.db import get_sessionmaker
from app.core.redis import get_arq_pool
from app.models import Invoice, InvoiceStatus

logger = structlog.get_logger(__name__)

QUEUE_DEPTH = Gauge("apilot_queue_depth", "Jobs waiting on the arq queue", ["queue"])
QUEUE_OLDEST_AGE = Gauge(
    "apilot_queue_oldest_job_age_seconds", "Age of the oldest queued job", ["queue"]
)
INVOICES_PENDING = Gauge(
    "apilot_invoices_pending", "Invoices resting in a non-terminal status", ["status"]
)
REVIEW_QUEUE_OLDEST = Gauge(
    "apilot_review_queue_oldest_seconds",
    "Age of the oldest invoice awaiting human review",
)

# Every status the gauge reports, refreshed together. Partial refreshes are the
# classic gauge bug: a status that drains to empty drops out of the GROUP BY, its
# series keeps its last non-zero value forever, and you chase a backlog that is not
# there.
_PENDING_STATUSES = (
    InvoiceStatus.RECEIVED,
    InvoiceStatus.EXTRACTING,
    InvoiceStatus.EXTRACTED,
    InvoiceStatus.MATCHING,
    InvoiceStatus.EXCEPTION,
    InvoiceStatus.HELD,
)
_REVIEW_STATUSES = (InvoiceStatus.EXCEPTION, InvoiceStatus.HELD)

_httpd: WSGIServer | None = None


def start_metrics_server(settings: Settings) -> None:
    global _httpd
    if _httpd is not None or not settings.worker_metrics_enabled:
        return
    try:
        _httpd, _ = start_http_server(settings.worker_metrics_port)
    except OSError:
        # An exporter must never stop the worker doing its job. `arq --watch` restarts
        # often enough in development to hit a socket still in TIME_WAIT, and a raise
        # here would turn that into a crash loop.
        logger.exception("worker_metrics_server_unavailable", port=settings.worker_metrics_port)
        return
    logger.info("worker_metrics_server_started", port=settings.worker_metrics_port)


def stop_metrics_server() -> None:
    global _httpd
    if _httpd is not None:
        _httpd.shutdown()
        _httpd.server_close()
    _httpd = None


async def refresh_gauges() -> None:
    """Sample the levels a counter cannot describe: depth, age, backlog."""
    pool = await get_arq_pool()
    queue = pool.default_queue_name
    now_ms = time.time() * 1000

    QUEUE_DEPTH.labels(queue=queue).set(await pool.zcard(queue))
    oldest = await pool.zrange(queue, 0, 0, withscores=True)
    QUEUE_OLDEST_AGE.labels(queue=queue).set(
        max(0.0, (now_ms - oldest[0][1]) / 1000) if oldest else 0.0
    )

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Invoice.status, func.count())
                .where(Invoice.status.in_(_PENDING_STATUSES))
                .group_by(Invoice.status)
            )
        ).all()
        counts: dict[InvoiceStatus, int] = dict(rows)  # type: ignore[arg-type]
        for status in _PENDING_STATUSES:
            INVOICES_PENDING.labels(status=status.value).set(counts.get(status, 0))

        oldest_review = await session.scalar(
            select(func.extract("epoch", func.now() - Invoice.decided_at))
            .where(Invoice.status.in_(_REVIEW_STATUSES), Invoice.decided_at.is_not(None))
            .order_by(Invoice.decided_at.asc())
            .limit(1)
        )
        REVIEW_QUEUE_OLDEST.set(float(oldest_review or 0.0))


async def gauge_refresh_loop(settings: Settings) -> None:
    """A plain asyncio task, deliberately — not an arq cron.

    A cron that measures the queue would enqueue onto the queue it measures, and arq
    runs a cron on exactly one replica, so every other replica would export the
    gauge's unset default of zero. A task runs on every replica and each reports the
    same true global value, which `max by (queue)` then collapses honestly.
    """
    while True:
        try:
            await refresh_gauges()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a transient database blip kill the loop: a gauge that stops
            # being refreshed does not go stale visibly, it freezes at its last value
            # and lies quietly.
            logger.exception("gauge_refresh_failed")
        await asyncio.sleep(settings.metrics_refresh_interval_s)
