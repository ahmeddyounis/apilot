"""Work must not grow with the corpus.

Statement counts and result-set sizes, not timings: these are properties of the code
rather than of the machine the suite happens to run on, so a failure means someone
reintroduced an unbounded query — which is actionable — rather than that CI was busy.
"""

import httpx
import pytest
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models import Invoice, InvoiceStatus
from tests.integration.volume.conftest import CORPUS_SIZE, StatementCounter

pytestmark = pytest.mark.integration


async def test_review_queue_page_is_one_page_of_rows(
    client: httpx.AsyncClient, full_access_key: str, corpus: int
) -> None:
    response = await client.get(
        "/api/v1/exceptions",
        params={"limit": 25},
        headers={"X-API-Key": full_access_key},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 25
    # The queue is much larger than the page, and the page is what got loaded.
    assert body["total_count"] >= CORPUS_SIZE // 4


async def test_deep_pagination_is_refused_rather_than_served_slowly(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """Offsetting past ten thousand re-sorts the whole queue to discard the result.
    A review queue that long is a staffing problem, not a paging one."""
    response = await client.get(
        "/api/v1/exceptions",
        params={"offset": 10_001},
        headers={"X-API-Key": full_access_key},
    )
    assert response.status_code == 422


async def test_duplicate_screening_cost_does_not_grow_with_the_corpus(corpus: int) -> None:
    """The exact-key check bounds both the rows it examines and the columns it loads.

    It used to select whole ORM objects — extraction, validation_results and
    fraud_flags JSONB included — for every invoice sharing an invoice number, with no
    limit, once per ingested invoice.
    """
    from app.services.dedup import _DUP_CANDIDATE_LIMIT

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Invoice.id, Invoice.vendor_id, Invoice.vendor_name, Invoice.status)
                .where(
                    Invoice.normalized_invoice_number.is_not(None),
                    Invoice.status.not_in(
                        (InvoiceStatus.EXTRACTION_FAILED, InvoiceStatus.REJECTED)
                    ),
                )
                .limit(_DUP_CANDIDATE_LIMIT)
            )
        ).all()

    assert len(rows) <= _DUP_CANDIDATE_LIMIT
    assert len(rows[0]) == 4  # four columns, not a hydrated Invoice


async def test_analytics_runs_a_fixed_number_of_statements(
    client: httpx.AsyncClient, full_access_key: str, corpus: int
) -> None:
    """Seven aggregates over a bounded window — the count must not track the data."""
    with StatementCounter() as counter:
        response = await client.get(
            "/api/v1/analytics/summary", headers={"X-API-Key": full_access_key}
        )

    assert response.status_code == 200
    assert response.json()["window"]["since"], "the resolved window must be reported"
    assert counter.count <= 15, f"{counter.count} statements for one summary"


async def test_export_marking_scales_by_chunk_not_by_round_trip(
    client: httpx.AsyncClient, full_access_key: str, corpus: int
) -> None:
    """Marking used to cost three round trips per invoice inside one transaction that
    held a row lock on every one of them for the length of the request."""
    async with get_sessionmaker()() as session:
        approved = await session.scalar(
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.status == InvoiceStatus.APPROVED)
        )
    assert approved and approved >= 50

    response = await client.post(
        "/api/v1/exports", params={"format": "csv"}, headers={"X-API-Key": full_access_key}
    )
    assert response.status_code == 200

    async with get_sessionmaker()() as session:
        still_approved = await session.scalar(
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.status == InvoiceStatus.APPROVED)
        )
    # Everything with line items got marked; the corpus rows have none, so they stay.
    assert still_approved == approved
