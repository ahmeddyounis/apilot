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

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import Invoice, InvoiceStatus
from app.services.state_machine import ALLOWED
from app.worker import _RESUMABLE_STATUSES, _TERMINAL_FOR_STATUS, requeue_stale
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import upload

pytestmark = pytest.mark.integration


async def age_invoice(invoice_id: UUID, *, minutes: int, attempts: int = 0) -> None:
    """Backdate updated_at past the cron's cutoff."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(
                updated_at=datetime.now(UTC) - timedelta(minutes=minutes),
                extraction_attempts=attempts,
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
        attempts=settings.pipeline_max_tries,
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
