import httpx
import pytest

from app.core.db import get_sessionmaker
from app.models import Invoice, InvoiceStatus
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import run_scenario

pytestmark = pytest.mark.integration


async def test_corrupt_totals_route_to_exception(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    invoice = await run_scenario(
        client, full_access_key, "extract-corrupt-total", minimal_pdf("corrupt-total")
    )
    assert invoice.status is InvoiceStatus.EXCEPTION
    assert invoice.exception_reason == "validation"
    assert invoice.decided_at is not None
    assert invoice.validation_results is not None
    assert invoice.validation_results["passed"] is False
    hard = {
        f["rule_id"] for f in invoice.validation_results["findings"] if f["severity"] == "hard_fail"
    }
    assert "V-003" in hard


async def test_exceptions_queue_lists_and_sorts(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    corrupt = await run_scenario(
        client, full_access_key, "extract-corrupt-total", minimal_pdf("queue-corrupt")
    )
    clean = await run_scenario(client, full_access_key, "extract-happy", minimal_pdf("queue-clean"))
    assert clean.status is InvoiceStatus.EXTRACTED  # validation passed, not queued

    response = await client.get("/api/v1/exceptions", headers={"X-API-Key": full_access_key})
    assert response.status_code == 200
    page = response.json()
    assert page["total_count"] == 1
    item = page["items"][0]
    assert item["id"] == str(corrupt.id)
    assert item["status"] == "exception"
    assert item["failed_rules"] == ["V-003"]
    assert item["total"] == "600.00"

    # min_total filter excludes it
    filtered = await client.get(
        "/api/v1/exceptions",
        params={"min_total": "1000"},
        headers={"X-API-Key": full_access_key},
    )
    assert filtered.json()["total_count"] == 0


async def test_happy_invoice_records_passing_validation(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    invoice = await run_scenario(
        client, full_access_key, "extract-happy", minimal_pdf("validation-happy")
    )
    assert invoice.status is InvoiceStatus.EXTRACTED
    assert invoice.validation_results is not None
    assert invoice.validation_results["passed"] is True
    # Vendor master is empty → the unknown-vendor flag annotates without gating.
    flags = {
        f["rule_id"] for f in invoice.validation_results["findings"] if f["severity"] == "flag"
    }
    assert "V-012" in flags

    async with get_sessionmaker()() as session:
        refreshed = await session.get(Invoice, invoice.id)
        assert refreshed is not None
        assert refreshed.exception_reason is None
