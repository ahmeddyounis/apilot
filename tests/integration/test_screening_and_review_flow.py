from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.normalize import normalize_vendor_name
from app.llm.fake import scenario
from app.models import FieldCorrection, Invoice, InvoiceStatus, Vendor
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


async def seed_vendor(name: str) -> None:
    """Register a vendor so extraction can link invoices to it.

    Vendor rows are otherwise only created through the PO endpoint, and an invoice
    with no vendor_id skips the payment-fingerprint checks entirely.
    """
    async with get_sessionmaker()() as session:
        session.add(Vendor(name=name, normalized_name=normalize_vendor_name(name)))
        await session.commit()


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
    # A second, unconsumed authorisation for the corrected invoice to bill against.
    # Pointing it back at PO-2231 would now be caught as over-consumption, since the
    # first invoice already used that purchase order up.
    await seed_po_and_receipt(client, full_access_key, po_number="PO-2232")
    await run_scenario(client, full_access_key, "extract-po-clean", minimal_pdf("rerun-a"))
    held = await run_scenario(client, full_access_key, "extract-dup-resub", minimal_pdf("rerun-b"))
    assert held.status is InvoiceStatus.HELD

    response = await client.post(
        f"/api/v1/invoices/{held.id}/review",
        headers={"X-API-Key": full_access_key},
        json={
            "action": "correct",
            "corrections": [{"field_path": "po_number", "new_value": "PO-2232"}],
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

    # GET previews without touching status; POST is the checkout that marks the
    # batch delivered, so the two are asserted separately.
    preview = await client.get(
        "/api/v1/exports",
        params={"format": "csv"},
        headers={"X-API-Key": full_access_key},
    )
    assert preview.status_code == 200
    assert (await get_invoice(approved.id)).status is InvoiceStatus.APPROVED

    export = await client.post(
        "/api/v1/exports",
        params={"format": "csv"},
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


async def test_flagged_bank_account_is_not_trusted_until_a_human_approves(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """F-110 has to survive a second attempt to be worth anything.

    Screening used to record every unseen fingerprint, including the one it had
    just raised F-110 on — so the attacker's account joined the vendor's trusted
    set at the moment of the alarm and the follow-up invoice on that same account
    passed clean. The control fired once per attacker, then disarmed itself.
    """
    await seed_vendor("Riverside Metals Ltd")
    legit = await run_scenario(
        client, full_access_key, "extract-bank-legit", minimal_pdf("bank-legit")
    )
    # F-100 (first invoice from this vendor) is expected; a bank change is not.
    assert "F-110" not in {flag["code"] for flag in legit.fraud_flags or []}

    switched = await run_scenario(
        client, full_access_key, "extract-bank-switched", minimal_pdf("bank-switch-1")
    )
    assert switched.status is InvoiceStatus.HELD
    assert "F-110" in {flag["code"] for flag in switched.fraud_flags or []}

    # The attacker's second invoice, same account: still caught.
    repeat = await run_scenario(
        client, full_access_key, "extract-bank-switched-again", minimal_pdf("bank-switch-2")
    )
    assert "F-110" in {flag["code"] for flag in repeat.fraud_flags or []}, (
        "the flagged account became trusted without any human approving it"
    )
    assert repeat.status is InvoiceStatus.HELD


async def test_approving_a_bank_change_trusts_the_new_account(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """The legitimate other half: vendors do change banks, and a reviewer saying
    so is the evidence screening cannot produce on its own."""
    await seed_vendor("Riverside Metals Ltd")
    await run_scenario(client, full_access_key, "extract-bank-legit", minimal_pdf("bank-ok"))
    switched = await run_scenario(
        client, full_access_key, "extract-bank-switched", minimal_pdf("bank-change")
    )
    assert switched.status is InvoiceStatus.HELD

    response = await client.post(
        f"/api/v1/invoices/{switched.id}/review",
        headers={"X-API-Key": full_access_key},
        json={"action": "approve", "note": "called the vendor, the change is real"},
    )
    assert response.status_code == 200

    after = await run_scenario(
        client, full_access_key, "extract-bank-switched-again", minimal_pdf("bank-after")
    )
    assert "F-110" not in {flag["code"] for flag in after.fraud_flags or []}, (
        "an approved bank change should stop flagging"
    )
