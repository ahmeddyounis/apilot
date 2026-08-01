# Observability

## What was wrong

`setup_metrics` is called from `create_app`, so only the API ever served `/metrics`.
But `apilot_invoice_transitions_total` and `apilot_llm_cost_usd_total` are
incremented almost entirely inside the **worker** — every pipeline transition, every
LLM call — and those increments landed in an in-process registry nothing scraped.
What `/metrics` actually published was the handful of transitions the API performs
itself: review decisions, and marking a batch exported.

So the metric named for the pipeline described the review desk, and the cost counter
read `$0.00` while the worker spent money. That is worse than having no metric,
because you would have trusted it.

## How the worker exports

`prometheus_client.start_http_server` on a **daemon thread**, started from arq's
`on_startup` (`app/core/worker_metrics.py`). Port 9110.

Not the worker's own event loop: that loop runs Hungarian assignment and, until
recently, ONNX inference, so a loop-bound exporter would miss scrapes exactly when
the worker is busiest — and a scrape timeout reads as "worker down" at the moment
the numbers matter most. `prometheus_client` guards every value with a lock, so a
WSGI thread reading the registry while the loop writes to it is safe.

Not multiprocess mode: that exists to aggregate several processes behind *one*
scrape target. The API and worker are separate deployments with separate addresses
and no shared filesystem.

Not a pushgateway: pushed series are sticky, so a dead worker keeps publishing its
last values and `up{job="apilot-worker"}` stops meaning the worker is alive — which
is the signal you most want when the queue backs up.

## The metrics

| Name | Type | Labels | Incremented |
|---|---|---|---|
| `apilot_invoice_transitions_total` | counter | `from_status`, `to_status` | `state_machine.transition` |
| `apilot_llm_cost_usd_total` | counter | `model`, `purpose` | `cost_ledger.record_llm_call` |
| `apilot_pipeline_stage_duration_seconds` | histogram | `stage`, `outcome` | `pipeline.process_invoice_pipeline` |
| `apilot_llm_call_failures_total` | counter | `purpose`, `model`, `reason` | around the provider call itself |
| `apilot_queue_depth` | gauge | `queue` | worker refresh loop |
| `apilot_queue_oldest_job_age_seconds` | gauge | `queue` | worker refresh loop |
| `apilot_invoices_pending` | gauge | `status` | worker refresh loop |
| `apilot_review_queue_oldest_seconds` | gauge | — | worker refresh loop |
| `apilot_pipeline_requeues_total` | counter | — | `worker.requeue_stale` |
| `apilot_pipeline_abandoned_total` | counter | `terminal_status` | `worker.requeue_stale` |

Two details that are load-bearing rather than incidental:

**`outcome` is three-valued** — `ok`, `terminal`, `error`. An invoice routed to `held`
or `exception` is the system working, not a fault. Folding that into `error` would
make a healthy exception rate look like an outage; folding it into `ok` would hide a
stage that had stopped letting anything through.

**The failure counter wraps the provider call, not the ledger write.** A hard API
error propagates before any bookkeeping runs, so a rate-limit storm was invisible to
both the cost ledger and Prometheus — the telemetry went quiet during the incident it
existed to describe.

### What is deliberately not a metric

A metric answers *"is something wrong right now, and how bad"*. The database answers
*"what exactly happened to invoice X"*. Anything needing an invoice id, vendor, or PO
number to be useful stays in Postgres — a label with unbounded values is how a
metrics backend becomes a liability.

So: no per-invoice or per-vendor labels; no rate-limit-rejection counter
(`http_requests_total{status="429"}` already answers it); no per-graph-node duration
(already in `matching_results.trace` and mirrored into the audit log); no DB-pool
gauge yet — the real saturation signal at this scale is `http_request_duration_seconds`
p95 climbing.

## Correlation, and why not OpenTelemetry

An `X-Request-ID` is honoured or minted per request, echoed on the response, bound to
structlog, passed to the arq job as a keyword argument, and defaulted into every
`audit_log` row from a contextvar. The rescue cron recovers the originating id from
the invoice's `upload` audit row, so correlation survives the retry path — the one you
most want to trace.

The result is a chain that ends in Postgres:

```
client X-Request-ID → response header → arq job kwarg → worker structlog
                    → audit_log.request_id (append-only, indexed)
```

**OpenTelemetry is deliberately not used.** What tracing buys is cross-service
causality; this system has two services and exactly one hop, and that hop is now
described by a record that is *better* than a trace for this domain — a trace backend
samples and expires spans in days, while an AP system's correlation record has to
survive an audit. The intra-request detail auto-instrumentation would add is already
recorded three times over: per-node durations in `matching_results.trace`,
per-call latency in `llm_calls.latency_ms`, and the stage histogram above.

The honest counter-argument is that OTel is the industry default and its absence in a
project claiming production competence is a fair question — which is why this
paragraph exists rather than the dependency. The seam, if a third service ever
appears: `app/core/logging.py:current_request_id()` and the `request_id` kwarg in
`enqueue_pipeline` are the two integration points; swapping the id for a W3C
`traceparent` and adding the FastAPI and SQLAlchemy instrumentations is roughly a
50-line change. An inbound `traceparent` is already adopted as the request id when
one is present, so an upstream tracer's ids line up with ours today.

## Running it locally

```bash
docker compose --profile observability up -d
open http://localhost:9090/alerts
```

`ops/prometheus/alerts.yml` is committed instead of a Grafana dashboard: it is the
artifact that says which numbers matter and what "bad" looks like, and CI verifies it
with `promtool check rules`, which a dashboard blob cannot be.
