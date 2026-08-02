"""Prometheus metrics incremented by both processes.

Worker-only metrics — the queue gauges and the exporter that serves them — live in
``app/core/worker_metrics.py`` instead. The split is load-bearing: a metric with no
labels reports a ``0`` from every process that imports its module, so declaring
``apilot_review_queue_oldest_seconds`` here would have the API pod publishing a
confident zero about a queue it has never looked at.

The rule for what belongs here at all: a metric answers *"is something wrong right
now, and how bad"*. Anything that needs an invoice id, vendor or PO number to be
useful is a database question, and stays in the database — a label with unbounded
values is how a metrics backend becomes a liability.
"""

import contextlib
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

INVOICE_TRANSITIONS = Counter(
    "apilot_invoice_transitions_total",
    "Invoice status transitions",
    ["from_status", "to_status"],
)

LLM_COST_USD = Counter(
    "apilot_llm_cost_usd_total",
    "Cumulative LLM spend in USD",
    ["model", "purpose"],
)

# Buckets are LLM-shaped. The prometheus default tops out at 10s, which would put
# every extraction in the +Inf bucket and tell you nothing.
STAGE_DURATION = Histogram(
    "apilot_pipeline_stage_duration_seconds",
    "Wall-clock duration of one pipeline stage",
    ["stage", "outcome"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
)

LLM_CALL_FAILURES = Counter(
    "apilot_llm_call_failures_total",
    "Provider calls that produced no usable response",
    ["purpose", "model", "reason"],
)

PIPELINE_REQUEUES = Counter(
    "apilot_pipeline_requeues_total",
    "Invoices re-queued by the stale-job cron",
)

PIPELINE_ABANDONED = Counter(
    "apilot_pipeline_abandoned_total",
    "Invoices the pipeline gave up on after exhausting its retry budget",
    ["terminal_status"],
)


@dataclass
class StageOutcome:
    """Three-valued on purpose.

    `terminal` is not a failure: an invoice routed to `held` or `exception` is the
    system working. Collapsing it into `error` would make a healthy exception rate
    look like an outage, and collapsing it into `ok` would hide a stage that stopped
    letting anything through.
    """

    value: str = "ok"


@contextmanager
def observe_stage(stage: str) -> Iterator[StageOutcome]:
    outcome = StageOutcome()
    started = time.monotonic()
    try:
        yield outcome
    except BaseException:
        outcome.value = "error"
        raise
    finally:
        STAGE_DURATION.labels(stage=stage, outcome=outcome.value).observe(
            time.monotonic() - started
        )


@asynccontextmanager
async def observe_llm_call(*, purpose: str, model: str) -> AsyncIterator[None]:
    """Wrap the provider call itself, not the ledger write that follows it.

    That placement is the whole point. A hard API error propagates out of the client
    *before* any bookkeeping runs, so a rate-limit storm used to be invisible to both
    the cost ledger and Prometheus — the telemetry went quiet exactly during the
    incident it existed to describe.
    """
    from app.llm.errors import classify_provider_error

    try:
        yield
    except Exception as exc:
        LLM_CALL_FAILURES.labels(
            purpose=purpose, model=model, reason=classify_provider_error(exc).reason
        ).inc()
        raise


def setup_metrics(app: FastAPI) -> None:
    instrumentator = Instrumentator(
        excluded_handlers=["/metrics", "/health", "/health/ready"],
    )
    # A previous app instance (tests build several) may already have registered the
    # HTTP metrics on the default registry; the endpoint below still serves them.
    with contextlib.suppress(ValueError):
        instrumentator.instrument(app)
    instrumentator.expose(app, endpoint="/metrics", include_in_schema=False)
