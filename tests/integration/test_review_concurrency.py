"""Concurrent review decisions must not interleave.

Two reviewers hitting the same invoice at once (one approves, one rejects) both
read `exception`, both find their transition legal, and both write. Without row
locking the invoice ends in whichever state committed last while the audit log
records both decisions as having succeeded — a contradictory financial record.
"""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from app.api.errors import InvalidStateError
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import Invoice, InvoiceStatus, ReviewAction
from app.services.review import load_for_review
from app.services.state_machine import transition
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import run_scenario

pytestmark = pytest.mark.integration


async def test_concurrent_approve_and_reject_do_not_both_succeed(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    invoice = await run_scenario(
        client, full_access_key, "extract-corrupt-total", minimal_pdf("race-review")
    )
    assert invoice.status is InvoiceStatus.EXCEPTION

    # Two independent clients, as two reviewers in different browsers would be.
    settings = get_settings()
    headers = {"X-API-Key": full_access_key}

    async def review(action: str) -> httpx.Response:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post(
                f"/api/v1/invoices/{invoice.id}/review",
                headers=headers,
                json={"action": action, "note": f"concurrent {action}"},
            )

    approve, reject = await asyncio.gather(review("approve"), review("reject"))
    statuses = sorted([approve.status_code, reject.status_code])

    # Exactly one decision may win; the loser must be told it lost.
    assert statuses == [200, 409], (
        f"both decisions were accepted: approve={approve.status_code} reject={reject.status_code}"
    )

    async with get_sessionmaker()() as session:
        refreshed = await session.get(Invoice, invoice.id)
        assert refreshed is not None
        assert refreshed.status in (InvoiceStatus.APPROVED, InvoiceStatus.REJECTED)

        # And only the winning decision is recorded as an action.
        recorded = await session.scalar(
            select(func.count())
            .select_from(ReviewAction)
            .where(ReviewAction.invoice_id == invoice.id)
        )
        assert recorded == 1, f"{recorded} review actions recorded for one decision"


async def test_locked_read_blocks_a_stale_second_decision(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """The interleave that the endpoint test can't force on a single event loop:
    both sessions read before either writes. The lock must serialize them."""
    invoice = await run_scenario(
        client, full_access_key, "extract-corrupt-total", minimal_pdf("forced-race")
    )
    assert invoice.status is InvoiceStatus.EXCEPTION

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as first:
        locked = await load_for_review(first, invoice.id)
        assert locked is not None
        await transition(first, locked, InvoiceStatus.APPROVED, actor="reviewer-1")

        async def second_decision() -> InvoiceStatus:
            async with sessionmaker() as other:
                # Blocks on the row lock until `first` commits, then sees APPROVED.
                stale = await load_for_review(other, invoice.id)
                assert stale is not None
                return stale.status

        pending = asyncio.create_task(second_decision())
        await asyncio.sleep(0.1)  # let the second session reach the lock and block
        assert not pending.done(), "second reader was not blocked by the row lock"
        await first.commit()
        observed = await pending

    assert observed is InvoiceStatus.APPROVED  # fresh read, not the stale snapshot
    with pytest.raises(InvalidStateError):
        async with sessionmaker() as third:
            decided = await load_for_review(third, invoice.id)
            assert decided is not None
            await transition(third, decided, InvoiceStatus.REJECTED, actor="reviewer-2")
