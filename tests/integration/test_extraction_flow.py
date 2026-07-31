from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.llm.fake import scenario
from app.models import AuditLog, Invoice, InvoiceLineItem, InvoiceStatus, LlmCall
from app.services.pipeline import process_invoice_pipeline
from tests.integration.conftest import minimal_pdf

pytestmark = pytest.mark.integration


async def upload(client: httpx.AsyncClient, key: str, idem: str, content: bytes) -> UUID:
    response = await client.post(
        "/api/v1/invoices",
        headers={"X-API-Key": key, "Idempotency-Key": idem},
        files={"file": ("invoice.pdf", content, "application/pdf")},
    )
    assert response.status_code == 202
    return UUID(response.json()["invoice_id"])


async def run_scenario(client: httpx.AsyncClient, key: str, name: str, content: bytes) -> Invoice:
    invoice_id = await upload(client, key, name, content)
    with scenario(name):
        await process_invoice_pipeline(invoice_id)
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        session.expunge(invoice)
        return invoice


async def test_happy_extraction(client: httpx.AsyncClient, full_access_key: str) -> None:
    invoice = await run_scenario(client, full_access_key, "extract-happy", minimal_pdf("happy"))
    assert invoice.status is InvoiceStatus.EXTRACTED
    assert invoice.vendor_name == "ACME Office Supply Co"
    assert invoice.total == Decimal("575.10")
    assert invoice.normalized_invoice_number == "INV20260042"
    assert invoice.extraction_mode == "single"
    assert invoice.extraction_attempts == 1
    assert invoice.extracted_at is not None
    assert invoice.field_confidence and invoice.field_confidence["total"] == 0.99
    assert invoice.extraction is not None
    assert "payment_details_raw" not in invoice.extraction

    async with get_sessionmaker()() as session:
        lines = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice.id)
            )
        ).all()
        assert [line.description for line in lines] == ["Widget A-200", "Widget B-100"]

        calls = (
            await session.scalars(select(LlmCall).where(LlmCall.invoice_id == invoice.id))
        ).all()
        assert len(calls) == 1
        assert calls[0].purpose == "extraction"
        assert calls[0].model == "fake-llm"
        assert calls[0].cost_usd == Decimal("0")

        attempt_events = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.invoice_id == invoice.id,
                    AuditLog.event_type == "extraction_attempt",
                )
            )
        ).all()
        assert len(attempt_events) == 1


async def test_retry_with_feedback_recovers(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    invoice = await run_scenario(client, full_access_key, "extract-retry", minimal_pdf("retry"))
    assert invoice.status is InvoiceStatus.EXTRACTED
    assert invoice.extraction_attempts == 2
    assert invoice.total == Decimal("470.00")

    async with get_sessionmaker()() as session:
        calls = (
            await session.scalars(select(LlmCall).where(LlmCall.invoice_id == invoice.id))
        ).all()
        assert len(calls) == 2


async def test_refusal_is_terminal_without_retry(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    invoice = await run_scenario(client, full_access_key, "extract-refusal", minimal_pdf("refusal"))
    assert invoice.status is InvoiceStatus.EXTRACTION_FAILED
    assert invoice.extraction_attempts == 1
    assert "declined" in (invoice.error_detail or "")

    async with get_sessionmaker()() as session:
        call = await session.scalar(select(LlmCall).where(LlmCall.invoice_id == invoice.id))
        assert call is not None and call.success is False


async def test_persistent_arithmetic_is_accepted_not_failed(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """A faithfully-extracted internally-inconsistent document must be persisted
    (validation routes it later) — never forced into extraction_failed."""
    invoice = await run_scenario(
        client, full_access_key, "extract-arith-persistent", minimal_pdf("sketchy")
    )
    assert invoice.status is InvoiceStatus.EXTRACTED
    assert invoice.extraction_attempts == 3  # 1 + 2 feedback retries, then accepted
    assert invoice.total == Decimal("575.10")
    assert invoice.payment_details_present is True
    assert invoice.extraction is not None
    assert "payment_details_raw" not in invoice.extraction  # never persisted


async def test_garbage_output_is_terminal_after_retries(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    invoice = await run_scenario(client, full_access_key, "extract-garbage", minimal_pdf("garbage"))
    assert invoice.status is InvoiceStatus.EXTRACTION_FAILED
    assert invoice.extraction_attempts == 3
    assert "schema" in (invoice.error_detail or "")


async def test_missing_fixture_is_honest_terminal(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    invoice_id = await upload(client, full_access_key, "no-fixture", minimal_pdf("unknown-content"))
    await process_invoice_pipeline(invoice_id)
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.status is InvoiceStatus.EXTRACTION_FAILED
        assert "fixture" in (invoice.error_detail or "")
