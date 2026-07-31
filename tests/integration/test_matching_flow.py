import httpx
import pytest
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models import AuditLog, InvoiceStatus, LlmCall
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import run_scenario

pytestmark = pytest.mark.integration


async def seed_po_and_receipt(
    client: httpx.AsyncClient,
    key: str,
    *,
    po_number: str = "PO-2231",
    quantity: str = "10",
    unit_price: str = "47.00",
    received: str | None = "10",
) -> None:
    response = await client.post(
        "/api/v1/purchase-orders",
        headers={"X-API-Key": key},
        json={
            "po_number": po_number,
            "vendor_name": "ACME Office Supply Co",
            "currency": "USD",
            "order_date": "2026-06-20",
            "line_items": [
                {"description": "Widget A-200", "quantity": quantity, "unit_price": unit_price}
            ],
        },
    )
    assert response.status_code == 201, response.text
    if received is not None:
        receipt = await client.post(
            "/api/v1/receipts",
            headers={"X-API-Key": key},
            json={
                "receipt_number": f"RCV-{po_number}",
                "po_number": po_number,
                "received_date": "2026-06-25",
                "line_items": [{"po_line_no": 1, "quantity_received": received}],
            },
        )
        assert receipt.status_code == 201, receipt.text


async def matching_llm_calls(invoice_id: object) -> int:
    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(LlmCall)
            .where(LlmCall.invoice_id == invoice_id, LlmCall.purpose == "matching")
        )
    return int(count or 0)


async def test_clean_match_auto_approves(client: httpx.AsyncClient, full_access_key: str) -> None:
    await seed_po_and_receipt(client, full_access_key)
    invoice = await run_scenario(
        client, full_access_key, "extract-po-clean", minimal_pdf("po-clean")
    )
    assert invoice.status is InvoiceStatus.APPROVED
    assert invoice.decided_at is not None
    assert invoice.vendor_id is not None  # matched the seeded vendor master row

    response = await client.get(
        f"/api/v1/invoices/{invoice.id}/matching", headers={"X-API-Key": full_access_key}
    )
    assert response.status_code == 200
    result = response.json()
    verdict = result["verdict"]
    assert verdict["verdict"] == "auto_approve"
    assert verdict["auto_approve_eligible"] is True
    assert all(verdict["checks"].values())
    assert verdict["discrepancies"] == []
    assert verdict["po_number"] == "PO-2231"
    assert verdict["po_resolution"] == "by_number"

    nodes = [event["node"] for event in result["trace"]]
    assert nodes == ["po_lookup", "line_align", "compute_diffs", "assemble_verdict"]
    assert await matching_llm_calls(invoice.id) == 0  # clean paths never spend tokens


async def test_overbilled_quantity_is_exception_with_spec_impact(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    await seed_po_and_receipt(client, full_access_key)
    invoice = await run_scenario(
        client, full_access_key, "extract-po-overbill", minimal_pdf("po-overbill")
    )
    assert invoice.status is InvoiceStatus.EXCEPTION
    assert invoice.exception_reason == "matching:quantity_mismatch"

    response = await client.get(
        f"/api/v1/invoices/{invoice.id}/matching", headers={"X-API-Key": full_access_key}
    )
    verdict = response.json()["verdict"]
    assert verdict["checks"]["quantity_match"] is False
    assert verdict["checks"]["receipt_match"] is True
    assert verdict["checks"]["price_match"] is True
    (discrepancy,) = verdict["discrepancies"]
    assert discrepancy["type"] == "quantity_mismatch"
    assert discrepancy["invoice_qty"].startswith("12")
    assert discrepancy["po_qty"].startswith("10")
    assert discrepancy["impact"]["amount"] == "94.00"
    assert await matching_llm_calls(invoice.id) == 0


async def test_missing_po_short_circuits_without_llm(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    await seed_po_and_receipt(client, full_access_key)  # exists, but invoice cites PO-9999
    invoice = await run_scenario(
        client, full_access_key, "extract-po-missing", minimal_pdf("po-missing")
    )
    assert invoice.status is InvoiceStatus.EXCEPTION
    assert invoice.exception_reason == "missing_po"

    response = await client.get(
        f"/api/v1/invoices/{invoice.id}/matching", headers={"X-API-Key": full_access_key}
    )
    result = response.json()
    verdict = result["verdict"]
    assert verdict["checks"]["po_found"] is False
    assert verdict["discrepancies"][0]["type"] == "missing_po"
    nodes = [event["node"] for event in result["trace"]]
    assert nodes == ["po_lookup", "assemble_verdict"]
    assert await matching_llm_calls(invoice.id) == 0


async def test_price_drift_triggers_tool_using_investigation(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    await seed_po_and_receipt(client, full_access_key)
    invoice = await run_scenario(
        client, full_access_key, "extract-po-pricedrift", minimal_pdf("po-pricedrift")
    )
    assert invoice.status is InvoiceStatus.EXCEPTION
    assert invoice.exception_reason == "matching:price_mismatch"

    response = await client.get(
        f"/api/v1/invoices/{invoice.id}/matching", headers={"X-API-Key": full_access_key}
    )
    result = response.json()
    verdict = result["verdict"]
    (discrepancy,) = [d for d in verdict["discrepancies"] if d["type"] == "price_mismatch"]
    assert discrepancy["impact"]["amount"] == "40.00"
    assert "vendor query" in verdict["agent_summary"]  # written by the (fake) LLM

    nodes = [event["node"] for event in result["trace"]]
    assert "investigate" in nodes
    assert nodes.count("compute_diffs") == 2  # diffs recomputed after investigation
    # Two assistant turns: one tool call + one final answer.
    assert await matching_llm_calls(invoice.id) == 2

    async with get_sessionmaker()() as session:
        steps = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.invoice_id == invoice.id, AuditLog.event_type == "agent_step")
        )
        assert int(steps or 0) == len(nodes)


async def test_a_purchase_order_cannot_be_billed_twice(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """One authorisation, one delivery, two invoices.

    Each invoice used to be matched against the PO in isolation, so both saw the
    full 10-unit delivery, both passed every check, and both auto-approved — 940.00
    paid against a 470.00 order. Neither duplicate detection nor 3-way matching
    caught it: the invoices are genuinely different documents, and each one on its
    own is a perfect match.
    """
    await seed_po_and_receipt(client, full_access_key)

    first = await run_scenario(
        client, full_access_key, "extract-po-clean", minimal_pdf("bill-once")
    )
    assert first.status is InvoiceStatus.APPROVED

    second = await run_scenario(
        client, full_access_key, "extract-po-rebill", minimal_pdf("bill-twice")
    )

    assert second.status is InvoiceStatus.EXCEPTION, "the same PO was billed twice"
    response = await client.get(
        f"/api/v1/invoices/{second.id}/matching", headers={"X-API-Key": full_access_key}
    )
    verdict = response.json()["verdict"]
    assert "po_over_consumption" in {d["type"] for d in verdict["discrepancies"]}
