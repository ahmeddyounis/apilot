"""Inputs and outputs the API used to mishandle.

Each case here produced either a 500 from a well-formed request, or a file that
does something unexpected when a human opens it.
"""

import csv
import io

import httpx
import pytest

from app.core.config import get_settings
from app.models import InvoiceStatus
from tests.integration.conftest import minimal_pdf, mint_key
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


async def test_one_key_cannot_upload_correct_and_approve(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four-eyes control, when it is switched on.

    Without it, a single credential walks the entire fraud path in three requests:
    submit an invoice, rewrite its payee and total, approve it for payment. Every
    step is authorised, so the audit trail reads as orderly work.
    """
    monkeypatch.setattr(get_settings(), "require_separate_approver", True, raising=False)
    invoice = await run_scenario(
        client, full_access_key, "extract-corrupt-total", minimal_pdf("four-eyes")
    )
    headers = {"X-API-Key": full_access_key}

    corrected = await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers=headers,
        json={
            "action": "correct",
            "corrections": [{"field_path": "vendor_name", "new_value": "Payee I Control Ltd"}],
        },
    )
    assert corrected.status_code == 200  # editing is still allowed

    approved = await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers=headers,
        json={"action": "approve"},
    )
    assert approved.status_code == 409
    assert approved.json()["error"]["code"] == "separate_approver_required"

    # A second, independent reviewer can approve the same invoice.
    other_key, _ = await mint_key(["read", "review"], name="second-reviewer")
    second = await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers={"X-API-Key": other_key},
        json={"action": "approve"},
    )
    assert second.status_code == 200


async def test_separate_approver_is_off_by_default(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """A single-operator deployment would deadlock on the control — there would be
    nobody else to approve — so it stays opt-in."""
    assert get_settings().require_separate_approver is False
    invoice = await run_scenario(
        client, full_access_key, "extract-corrupt-total", minimal_pdf("solo-operator")
    )

    response = await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers={"X-API-Key": full_access_key},
        json={"action": "approve"},
    )
    assert response.status_code == 200


async def test_oversized_body_is_refused_before_it_is_read(
    client: httpx.AsyncClient,
) -> None:
    """FastAPI resolves the body before it solves dependencies, and Starlette's
    multipart parser spools file parts to disk with no cap of its own — so an
    anonymous POST used to reach disk in full and only then get its 401."""
    limit = get_settings().max_upload_bytes
    response = await client.post(
        "/api/v1/invoices",
        headers={"Content-Length": str(limit + 1), "Content-Type": "application/pdf"},
        content=b"",  # never sent; the declared size is what gets rejected
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_the_real_limit_is_still_the_streamed_bytes(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """Content-Length is a client claim, so the header check is an optimisation, not
    the control. A body that understates its size must still be caught downstream."""
    limit = get_settings().max_upload_bytes
    response = await client.post(
        "/api/v1/invoices",
        headers={"X-API-Key": full_access_key, "Idempotency-Key": "oversize-stream"},
        files={"file": ("big.pdf", b"%PDF-1.4\n" + b"0" * (limit + 1), "application/pdf")},
    )

    assert response.status_code == 413


async def test_a_broken_rate_limiter_returns_503_rather_than_hanging(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rate_limited()` is a dependency on nearly every endpoint, so an unreachable
    Redis decides whether the API responds at all. With no socket timeout and a
    black-holed connection it waited forever — while /health, which does no I/O by
    design, stayed green and kept the pod in service."""

    async def unreachable(*_: object, **__: object) -> None:
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr("app.core.ratelimit.RateLimiter.hit", unreachable)

    response = await client.get("/api/v1/exceptions", headers={"X-API-Key": full_access_key})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
