from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.llm.fake import scenario
from app.models import FieldCorrection, Invoice, InvoiceStatus
from app.services.pipeline import process_invoice_pipeline
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import run_scenario, upload
from tests.integration.test_matching_flow import seed_po_and_receipt

pytestmark = pytest.mark.integration


async def get_invoice(invoice_id: UUID) -> Invoice:
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        session.expunge(invoice)
        return invoice


async def test_exact_file_duplicate_is_held(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    await seed_po_and_receipt(client, full_access_key)
    original = await run_scenario(
        client, full_access_key, "extract-po-clean", minimal_pdf("dup-original")
    )
    assert original.status is InvoiceStatus.APPROVED

    # Same bytes, new idempotency key → a second invoice over the same file row.
    duplicate_id = await upload(
        client, full_access_key, "dup-second-key", minimal_pdf("dup-original")
    )
    with scenario("extract-po-clean"):
        await process_invoice_pipeline(duplicate_id)

    duplicate = await get_invoice(duplicate_id)
    assert duplicate.status is InvoiceStatus.HELD
    assert "DUP-EXACT" in (duplicate.hold_reason or "")
    codes = {flag["code"] for flag in duplicate.fraud_flags or []}
    assert "DUP-EXACT-FILE" in codes
    assert duplicate.risk_score is not None and duplicate.risk_score >= Decimal("0.5")


async def test_near_duplicate_new_number_is_held(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """Same content re-billed under a new invoice number — the case that costs
    real money — caught by embeddings + fuzzy cross-checks."""
    await seed_po_and_receipt(client, full_access_key)
    original = await run_scenario(
        client, full_access_key, "extract-po-clean", minimal_pdf("neardup-original")
    )
    assert original.status is InvoiceStatus.APPROVED

    resub = await run_scenario(
        client, full_access_key, "extract-dup-resub", minimal_pdf("neardup-resub")
    )
    assert resub.status is InvoiceStatus.HELD
    flags = {flag["code"]: flag for flag in resub.fraud_flags or []}
    assert "DUP-SUSPECT" in flags
    assert flags["DUP-SUSPECT"]["evidence"]["matched_invoice_id"] == str(original.id)


async def test_review_approve_releases_held_invoice(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    await seed_po_and_receipt(client, full_access_key)
    await run_scenario(client, full_access_key, "extract-po-clean", minimal_pdf("rel-original"))
    held = await run_scenario(
        client, full_access_key, "extract-dup-resub", minimal_pdf("rel-resub")
    )
    assert held.status is InvoiceStatus.HELD

    response = await client.post(
        f"/api/v1/invoices/{held.id}/review",
        headers={"X-API-Key": full_access_key},
        json={"action": "approve", "note": "verified with vendor: separate delivery"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    # A second approve is an honest 409, not a silent no-op.
    again = await client.post(
        f"/api/v1/invoices/{held.id}/review",
        headers={"X-API-Key": full_access_key},
        json={"action": "approve"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_transition"


async def test_review_correct_stores_ground_truth_and_revalidates(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    invoice = await run_scenario(
        client, full_access_key, "extract-corrupt-total", minimal_pdf("correct-me")
    )
    assert invoice.status is InvoiceStatus.EXCEPTION  # V-003 hard fail

    response = await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers={"X-API-Key": full_access_key},
        json={
            "action": "correct",
            "note": "total misprinted on document",
            "corrections": [{"field_path": "total", "new_value": "507.60"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "exception"  # human still owns the final approve
    assert body["corrections_applied"] == 1
    assert body["validation_passed"] is True

    async with get_sessionmaker()() as session:
        corrections = (
            await session.scalars(
                select(FieldCorrection).where(FieldCorrection.invoice_id == invoice.id)
            )
        ).all()
        assert len(corrections) == 1
        assert corrections[0].field_path == "total"
        assert corrections[0].old_value == "600.00"
        assert corrections[0].new_value == "507.60"

    unknown_field = await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers={"X-API-Key": full_access_key},
        json={"action": "correct", "corrections": [{"field_path": "nope", "new_value": 1}]},
    )
    assert unknown_field.status_code == 422
    assert unknown_field.json()["error"]["code"] == "invalid_correction"


async def test_correct_with_rerun_matching_reaches_approval(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    await seed_po_and_receipt(client, full_access_key)
    await run_scenario(client, full_access_key, "extract-po-clean", minimal_pdf("rerun-a"))
    held = await run_scenario(client, full_access_key, "extract-dup-resub", minimal_pdf("rerun-b"))
    assert held.status is InvoiceStatus.HELD

    response = await client.post(
        f"/api/v1/invoices/{held.id}/review",
        headers={"X-API-Key": full_access_key},
        json={
            "action": "correct",
            "corrections": [{"field_path": "po_number", "new_value": "PO-2231"}],
            "rerun_matching": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "matching"

    await process_invoice_pipeline(held.id)  # worker picks it up from MATCHING
    final = await get_invoice(held.id)
    assert final.status is InvoiceStatus.APPROVED
    assert final.fraud_flags == []  # human release cleared them (audited)


async def test_exports_and_analytics(client: httpx.AsyncClient, full_access_key: str) -> None:
    await seed_po_and_receipt(client, full_access_key)
    approved = await run_scenario(
        client, full_access_key, "extract-po-clean", minimal_pdf("export-clean")
    )
    assert approved.status is InvoiceStatus.APPROVED

    export = await client.get(
        "/api/v1/exports",
        params={"format": "csv", "mark_exported": "true"},
        headers={"X-API-Key": full_access_key},
    )
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    lines = [line for line in export.text.strip().splitlines() if line]
    assert lines[0].startswith("invoice_id,")
    assert len(lines) == 2  # header + one line item
    assert "ACME Office Supply Co" in lines[1]

    marked = await get_invoice(approved.id)
    assert marked.status is InvoiceStatus.EXPORTED
    assert marked.exported_at is not None

    qbo = await client.get(
        "/api/v1/exports",
        params={"format": "quickbooks", "include_exported": "true"},
        headers={"X-API-Key": full_access_key},
    )
    assert qbo.text.startswith("BillNo,Supplier,")

    empty = await client.get(
        "/api/v1/exports", params={"format": "csv"}, headers={"X-API-Key": full_access_key}
    )
    assert len([line for line in empty.text.strip().splitlines() if line]) == 1  # header only

    analytics = await client.get(
        "/api/v1/analytics/summary", headers={"X-API-Key": full_access_key}
    )
    assert analytics.status_code == 200
    summary = analytics.json()
    assert summary["invoices"]["by_status"]["exported"] == 1
    assert summary["auto_approval_rate"] == 1.0  # approved by the agent, untouched by humans
    assert summary["cycle_time_seconds"]["avg"] is not None
    assert "fake-llm" in summary["llm_cost"]["by_model"]
