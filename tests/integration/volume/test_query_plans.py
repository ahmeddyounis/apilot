"""The hot queries must reach for an index rather than reading the table."""

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import Invoice, InvoiceStatus
from tests.integration.volume.conftest import explain

pytestmark = pytest.mark.integration


async def test_duplicate_check_by_invoice_number_uses_an_index(corpus: int) -> None:
    """Runs once per invoice on the ingest path. The only index covering this column
    led with vendor_id, which the query does not constrain, so it was unusable and the
    scan was sequential — growing with every invoice the system had ever seen."""
    statement = (
        select(Invoice.id, Invoice.vendor_id, Invoice.vendor_name, Invoice.status)
        .where(
            Invoice.normalized_invoice_number == "INV7",
            Invoice.status.not_in((InvoiceStatus.EXTRACTION_FAILED, InvoiceStatus.REJECTED)),
        )
        .limit(25)
    )
    async with get_sessionmaker()() as session:
        plan = await explain(session, statement)

    assert "Seq Scan on invoices" not in plan, plan
    assert "ix_invoices_normalized_invoice_number" in plan, plan


async def test_review_queue_sort_uses_the_partial_index(corpus: int) -> None:
    """The sort key is a stored generated column now. Computed inline it was a CASE
    over two coalesced mutable columns, which no index can serve, so every page
    recomputed it for every queued invoice and then top-N sorted."""
    statement = (
        select(Invoice.id)
        .where(Invoice.status.in_((InvoiceStatus.EXCEPTION, InvoiceStatus.HELD)))
        .order_by(Invoice.review_priority.desc(), Invoice.received_at.asc())
        .limit(25)
    )
    async with get_sessionmaker()() as session:
        plan = await explain(session, statement)

    assert "ix_invoices_review_queue" in plan, plan
    # An index scan already returns rows in order; a Sort node means it did not.
    assert "Sort" not in plan, plan
