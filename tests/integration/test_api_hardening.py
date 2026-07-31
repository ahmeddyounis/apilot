"""Inputs and outputs the API used to mishandle.

Each case here produced either a 500 from a well-formed request, or a file that
does something unexpected when a human opens it.
"""

import csv
import io

import httpx
import pytest

from app.models import InvoiceStatus
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import run_scenario

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("field_path", "new_value"),
    [
        ("total", "NaN"),  # Decimal() accepts it; every later comparison is False
        ("subtotal", "nan"),
        ("total", "Infinity"),
        ("total", "1e40"),  # parses, then overflows NUMERIC(14,2) at commit
        ("currency", "TOOLONG"),  # exceeds varchar(3)
        ("line_items[1].quantity", "NaN"),
        ("vendor_name", None),  # bare str() stored the text "None"
        ("vendor_name", {"evil": 1}),  # ...and "{'evil': 1}"
    ],
)
async def test_junk_corrections_are_rejected_not_crashes(
    client: httpx.AsyncClient, full_access_key: str, field_path: str, new_value: object
) -> None:
    """A review-scoped key could 500 the API with one JSON body, and could write
    values that are not text into the corrections that feed the eval dataset."""
    invoice = await run_scenario(
        client,
        full_access_key,
        "extract-corrupt-total",
        minimal_pdf(f"junk-{field_path}-{new_value}"),
    )

    response = await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers={"X-API-Key": full_access_key},
        json={
            "action": "correct",
            "corrections": [{"field_path": field_path, "new_value": new_value}],
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] in {"invalid_correction", "validation_error"}


async def test_export_neutralises_spreadsheet_formulas(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """vendor_name and line descriptions are written by whoever sends the invoice.
    A leading = or @ makes the cell a live formula in Excel, Sheets and QuickBooks."""
    invoice = await run_scenario(
        client, full_access_key, "extract-formula-injection", minimal_pdf("formula")
    )
    assert invoice.status in (InvoiceStatus.EXCEPTION, InvoiceStatus.HELD)

    await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers={"X-API-Key": full_access_key},
        json={"action": "approve"},
    )

    export = await client.get(
        "/api/v1/exports", params={"format": "csv"}, headers={"X-API-Key": full_access_key}
    )
    assert export.status_code == 200

    rows = list(csv.reader(io.StringIO(export.text)))
    live = [c for row in rows for c in row if c.startswith(("=", "+", "-", "@"))]
    assert live == [], f"cells reach the spreadsheet as live formulas: {live}"

    # Neutralised, not censored — a reviewer still sees what the vendor wrote.
    flat = [c for row in rows for c in row]
    assert "'=cmd|'/C calc.exe'!A0" in flat


async def test_out_of_queue_status_filter_is_rejected(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """`?status=approved` used to return the whole exception queue, which reads as
    an answer to the question rather than a silently different one."""
    response = await client.get(
        "/api/v1/exceptions", params={"status": "approved"}, headers={"X-API-Key": full_access_key}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_filter"

    ok = await client.get(
        "/api/v1/exceptions", params={"status": "held"}, headers={"X-API-Key": full_access_key}
    )
    assert ok.status_code == 200
