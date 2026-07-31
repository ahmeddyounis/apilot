"""Prometheus metrics: HTTP instrumentation plus domain counters."""

import contextlib

from fastapi import FastAPI
from prometheus_client import Counter
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


def setup_metrics(app: FastAPI) -> None:
    instrumentator = Instrumentator(
        excluded_handlers=["/metrics", "/health", "/health/ready"],
    )
    # A previous app instance (tests build several) may already have registered the
    # HTTP metrics on the default registry; the endpoint below still serves them.
    with contextlib.suppress(ValueError):
        instrumentator.instrument(app)
    instrumentator.expose(app, endpoint="/metrics", include_in_schema=False)
