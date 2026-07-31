import asyncio
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import AuditLog, Invoice, InvoiceStatus
from app.services.pipeline import process_invoice_pipeline

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.4 upload flow test invoice"


def _upload_kwargs(key: str, idem: str, content: bytes = PDF) -> dict:
    return {
        "headers": {"X-API-Key": key, "Idempotency-Key": idem},
        "files": {"file": ("invoice.pdf", content, "application/pdf")},
    }


async def test_upload_replay_conflict_and_detail(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    first = await client.post("/api/v1/invoices", **_upload_kwargs(full_access_key, "idem-1"))
    assert first.status_code == 202
    body = first.json()
    assert body["status"] == "received"
    invoice_id = body["invoice_id"]

    replay = await client.post("/api/v1/invoices", **_upload_kwargs(full_access_key, "idem-1"))
    assert replay.status_code == 202
    assert replay.json()["invoice_id"] == invoice_id
    assert replay.headers.get("Idempotency-Replay") == "true"

    conflict = await client.post(
        "/api/v1/invoices",
        **_upload_kwargs(full_access_key, "idem-1", b"%PDF-1.4 different bytes"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_reuse"

    detail = await client.get(
        f"/api/v1/invoices/{invoice_id}", headers={"X-API-Key": full_access_key}
    )
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["status"] == "received"
    assert payload["file"]["mime"] == "application/pdf"
    assert payload["line_items"] == []


async def test_concurrent_same_key_uploads_converge(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    results = await asyncio.gather(
        client.post("/api/v1/invoices", **_upload_kwargs(full_access_key, "race-1")),
        client.post("/api/v1/invoices", **_upload_kwargs(full_access_key, "race-1")),
    )
    assert {r.status_code for r in results} == {202}
    ids = {r.json()["invoice_id"] for r in results}
    assert len(ids) == 1


async def test_pipeline_runs_to_honest_terminal_state(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    response = await client.post("/api/v1/invoices", **_upload_kwargs(full_access_key, "pipe-1"))
    invoice_id = UUID(response.json()["invoice_id"])

    await process_invoice_pipeline(invoice_id)

    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.status is InvoiceStatus.EXTRACTION_FAILED
        assert invoice.error_detail
        assert invoice.decided_at is not None

        events = (
            await session.scalars(
                select(AuditLog).where(AuditLog.invoice_id == invoice_id).order_by(AuditLog.id)
            )
        ).all()
        assert [event.event_type for event in events] == [
            "upload",
            "state_transition",
            "state_transition",
        ]
        assert events[-1].to_status == "extraction_failed"


async def test_same_document_under_two_keys_gets_two_invoices(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """The most ordinary thing an AP inbox does: the same document arrives twice.

    Different idempotency keys mean these are two genuine requests, not a replay,
    so each is entitled to its own invoice over the shared content-addressed file
    row — and the duplicate screen is what decides whether the second gets paid.
    Racing them used to hand one caller a 500, because the loser of the
    invoice_files.sha256 insert was only taught how to recover from losing the
    *idempotency-key* insert.
    """
    results = await asyncio.gather(
        client.post("/api/v1/invoices", **_upload_kwargs(full_access_key, "same-doc-a")),
        client.post("/api/v1/invoices", **_upload_kwargs(full_access_key, "same-doc-b")),
    )

    assert [r.status_code for r in results] == [202, 202], [r.text for r in results]
    ids = {r.json()["invoice_id"] for r in results}
    assert len(ids) == 2, "the same document under two keys should yield two invoices"

    # One file row, shared — content-addressed storage still deduplicates the bytes.
    async with get_sessionmaker()() as session:
        invoices = [await session.get(Invoice, UUID(i)) for i in ids]
        file_ids = {inv.file_id for inv in invoices if inv is not None}
        assert len(file_ids) == 1
