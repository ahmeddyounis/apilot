"""The stale-job cron must rescue every resumable status — including RECEIVED.

An invoice accepted while Redis was briefly unavailable was never enqueued. If the
cron doesn't cover RECEIVED, that invoice is silently orphaned forever despite the
client holding a 202.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import update

from app.api.errors import ConcurrentTransitionError
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import Invoice, InvoiceStatus
from app.services.state_machine import ALLOWED, transition
from app.worker import _RESUMABLE_STATUSES, _TERMINAL_FOR_STATUS, requeue_stale
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import upload

pytestmark = pytest.mark.integration


async def age_invoice(invoice_id: UUID, *, minutes: int, requeues: int = 0) -> None:
    """Backdate updated_at past the cron's cutoff."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(
                updated_at=datetime.now(UTC) - timedelta(minutes=minutes),
                pipeline_requeues=requeues,
            )
        )
        await session.commit()


async def status_of(invoice_id: UUID) -> InvoiceStatus:
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        return invoice.status


def test_every_resumable_status_has_a_terminal_mapping() -> None:
    assert set(_RESUMABLE_STATUSES) == set(_TERMINAL_FOR_STATUS)
    for source, terminal in _TERMINAL_FOR_STATUS.items():
        assert terminal in ALLOWED[source], f"{source.value} -> {terminal.value} not allowed"


async def test_received_invoice_is_requeued_not_orphaned(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate the enqueue failing at upload: the invoice must not be stranded."""
    enqueued: list[str] = []

    class StubPool:
        async def enqueue_job(self, _name: str, invoice_id: str, **_: Any) -> None:
            enqueued.append(invoice_id)

    async def stub_pool() -> StubPool:
        return StubPool()

    # Upload with a broken queue — the API still accepts the invoice.
    monkeypatch.setattr("app.services.invoices.get_arq_pool", _raise_queue_down)
    invoice_id = await upload(client, full_access_key, "orphan-1", minimal_pdf("orphan"))
    assert await status_of(invoice_id) is InvoiceStatus.RECEIVED

    # Nothing rescues it until the invoice is older than the cutoff.
    monkeypatch.setattr("app.worker.get_arq_pool", stub_pool)
    await requeue_stale({})
    assert enqueued == []

    await age_invoice(invoice_id, minutes=get_settings().stale_job_requeue_minutes + 1)
    await requeue_stale({})
    assert enqueued == [str(invoice_id)]
    assert await status_of(invoice_id) is InvoiceStatus.RECEIVED  # still pending, not lost


async def test_received_invoice_gives_up_honestly_after_max_tries(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubPool:
        async def enqueue_job(self, *_: Any, **__: Any) -> None:
            return None

    async def stub_pool() -> StubPool:
        return StubPool()

    monkeypatch.setattr("app.services.invoices.get_arq_pool", _raise_queue_down)
    invoice_id = await upload(client, full_access_key, "orphan-2", minimal_pdf("orphan2"))

    settings = get_settings()
    await age_invoice(
        invoice_id,
        minutes=settings.stale_job_requeue_minutes + 1,
        requeues=settings.pipeline_max_tries,
    )
    monkeypatch.setattr("app.worker.get_arq_pool", stub_pool)
    await requeue_stale({})

    # Terminal and honest: extraction never happened, and the record says so.
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.status is InvoiceStatus.EXTRACTION_FAILED
        assert "Abandoned after" in (invoice.error_detail or "")


async def _raise_queue_down() -> Any:
    raise ConnectionError("redis unavailable")


async def test_extraction_retries_do_not_consume_the_requeue_budget(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two counters measure different things and must not share a budget.

    `extraction_attempts` counts LLM calls, so an invoice that legitimately used
    retry-with-feedback lands at 3 of an allowed 4. While that was also the number
    the cron compared against `pipeline_max_tries`, such an invoice arrived at the
    matching stage nearly out of budget and was abandoned on the first rescue tick
    — with the untrue explanation that it had been through four pipeline attempts.
    """
    enqueued: list[str] = []

    class StubPool:
        async def enqueue_job(self, _name: str, invoice_id: str, **_: Any) -> str:
            enqueued.append(invoice_id)
            return "job-id"

    async def stub_pool() -> StubPool:
        return StubPool()

    monkeypatch.setattr("app.services.invoices.get_arq_pool", _raise_queue_down)
    invoice_id = await upload(client, full_access_key, "retried", minimal_pdf("retried"))

    settings = get_settings()
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(
                updated_at=datetime.now(UTC)
                - timedelta(minutes=settings.stale_job_requeue_minutes + 1),
                extraction_attempts=settings.pipeline_max_tries,  # spent on LLM calls
                pipeline_requeues=0,  # but never actually re-queued
            )
        )
        await session.commit()

    monkeypatch.setattr("app.worker.get_arq_pool", stub_pool)
    await requeue_stale({})

    assert enqueued == [str(invoice_id)], "a retried invoice was abandoned instead of rescued"
    assert await status_of(invoice_id) is InvoiceStatus.RECEIVED


async def test_requeue_does_not_count_a_job_arq_already_has(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """arq returns None when the job id is already queued. Counting that as an
    attempt let the cron exhaust an invoice whose original job hadn't started."""

    class BusyPool:
        async def enqueue_job(self, *_: Any, **__: Any) -> None:
            return None  # a job with this id is already on the queue

    async def busy_pool() -> BusyPool:
        return BusyPool()

    monkeypatch.setattr("app.services.invoices.get_arq_pool", _raise_queue_down)
    invoice_id = await upload(client, full_access_key, "queued", minimal_pdf("queued"))
    await age_invoice(invoice_id, minutes=get_settings().stale_job_requeue_minutes + 1)

    monkeypatch.setattr("app.worker.get_arq_pool", busy_pool)
    for _ in range(get_settings().pipeline_max_tries + 2):
        await requeue_stale({})

    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.pipeline_requeues == 0
        assert invoice.status is InvoiceStatus.RECEIVED, "killed while its job was still queued"


async def test_a_stale_decision_cannot_overwrite_a_fresher_one(
    client: httpx.AsyncClient, full_access_key: str
) -> None:
    """The matching stage runs for a while and never touches the invoice row, so
    its idea of the status goes stale by design. The cron can abandon the invoice
    in that window; the worker must not then approve it anyway.

    Before the compare-and-set in `transition`, the second write simply won: an
    invoice the system had publicly given up on ended `approved` for payment, still
    carrying the abandonment reason, with both outcomes in the append-only audit
    log that is supposed to record what actually happened.
    """
    invoice_id = await upload(client, full_access_key, "clobber", minimal_pdf("clobber"))
    sessionmaker = get_sessionmaker()

    # The worker loads the invoice and starts a long matching run.
    async with sessionmaker() as worker_session:
        worker_view = await worker_session.get(Invoice, invoice_id)
        assert worker_view is not None
        await transition(worker_session, worker_view, InvoiceStatus.EXTRACTING, actor="worker")
        await worker_session.commit()

        # Meanwhile the cron gives up on it and commits that decision.
        async with sessionmaker() as cron_session:
            cron_view = await cron_session.get(Invoice, invoice_id)
            assert cron_view is not None
            cron_view.error_detail = "Abandoned after 4 pipeline attempts"
            await transition(
                cron_session,
                cron_view,
                InvoiceStatus.EXTRACTION_FAILED,
                actor="worker",
                reason="pipeline_error",
            )
            await cron_session.commit()

        # The worker finishes and tries to record success against its stale view.
        with pytest.raises(ConcurrentTransitionError):
            await transition(worker_session, worker_view, InvoiceStatus.EXTRACTED, actor="worker")

    assert await status_of(invoice_id) is InvoiceStatus.EXTRACTION_FAILED
