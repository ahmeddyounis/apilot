"""Telemetry that describes what actually happened.

Two things were wrong. The pipeline's metrics were incremented in a process with no
scrape endpoint, so `/metrics` published a pipeline-shaped name whose value described
only the review desk. And the job carried nothing but an invoice id, so a worker
failure could be traced to *which key* uploaded the invoice but never to which
request.
"""

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.worker_metrics import refresh_gauges
from app.models import AuditLog, Invoice, InvoiceStatus
from app.services.state_machine import transition
from app.worker import process_invoice
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import upload

pytestmark = pytest.mark.integration


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_every_audit_row_carries_the_request_that_caused_it(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """The whole feature in one assertion: rows written by the API and rows written
    minutes later by the worker share the id of the HTTP request that started it."""
    response = await client.post(
        "/api/v1/invoices",
        headers={
            "X-API-Key": full_access_key,
            "Idempotency-Key": "corr-1",
            "X-Request-ID": "req-abc-123",
        },
        files={"file": ("invoice.pdf", minimal_pdf("correlation"), "application/pdf")},
    )
    assert response.status_code == 202
    assert response.headers["X-Request-ID"] == "req-abc-123"
    invoice_id = response.json()["invoice_id"]

    await process_invoice({}, invoice_id, request_id="req-abc-123")

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(AuditLog.event_type, AuditLog.request_id).where(
                    AuditLog.invoice_id == invoice_id
                )
            )
        ).all()

    assert rows, "the pipeline wrote no audit rows"
    assert {r.request_id for r in rows} == {"req-abc-123"}
    # Spans the boundary: an API-written row and worker-written rows.
    assert "upload" in {r.event_type for r in rows}
    assert "state_transition" in {r.event_type for r in rows}


async def test_enqueue_passes_the_request_id_to_the_job(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class CapturingPool:
        async def enqueue_job(self, _name: str, *args: object, **kwargs: object) -> str:
            captured.update(kwargs)
            return "job-id"

    async def pool() -> CapturingPool:
        return CapturingPool()

    monkeypatch.setattr("app.services.invoices.get_arq_pool", pool)
    await client.post(
        "/api/v1/invoices",
        headers={
            "X-API-Key": full_access_key,
            "Idempotency-Key": "corr-2",
            "X-Request-ID": "req-enqueue",
        },
        files={"file": ("invoice.pdf", minimal_pdf("enqueue-corr"), "application/pdf")},
    )

    assert captured["request_id"] == "req-enqueue"


async def test_pipeline_metrics_are_incremented_by_the_worker(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """Deltas, never absolutes: the prometheus registry is a process global and the
    rest of the suite has been incrementing these too."""
    name = "apilot_invoice_transitions_total"
    before = sample(name, from_status="received", to_status="extracting")
    stage_before = sample(
        "apilot_pipeline_stage_duration_seconds_count", stage="extraction", outcome="terminal"
    )

    invoice_id = await upload(client, full_access_key, "metrics-1", minimal_pdf("metrics"))
    await process_invoice({}, str(invoice_id))

    assert sample(name, from_status="received", to_status="extracting") == before + 1
    # No fixture for this content hash, so extraction ends terminally — which is a
    # normal outcome, not an error, and the label says so.
    assert (
        sample(
            "apilot_pipeline_stage_duration_seconds_count", stage="extraction", outcome="terminal"
        )
        == stage_before + 1
    )


async def test_gauges_zero_fill_every_status(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """A status that drains to empty drops out of the GROUP BY. Setting only what the
    query returned leaves the series frozen at its last non-zero value forever — a
    phantom backlog no amount of staring at the database explains."""
    invoice_id = await upload(client, full_access_key, "gauge-1", minimal_pdf("gauge"))
    await refresh_gauges()
    assert sample("apilot_invoices_pending", status="received") >= 1

    # Drain the status the way the pipeline does.
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.status is InvoiceStatus.RECEIVED
        await transition(session, invoice, InvoiceStatus.EXTRACTING, actor="test")
        await session.commit()

    await refresh_gauges()
    assert sample("apilot_invoices_pending", status="received") == 0
    assert sample("apilot_invoices_pending", status="extracting") >= 1


async def test_queue_depth_reflects_the_real_queue(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    await upload(client, full_access_key, "queued-1", minimal_pdf("queued-metric"))
    await refresh_gauges()

    assert sample("apilot_queue_depth", queue="arq:queue") >= 1
