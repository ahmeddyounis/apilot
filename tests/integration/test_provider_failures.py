"""How the pipeline behaves when the model provider itself fails.

Before this, a 429 or a timeout propagated out of the client *before* the cost-ledger
write: no ledger row, no audit row, no error surface, and an invoice sitting in
`extracting` until the rescue cron noticed fifteen minutes later. Four paid attempts
could leave two rows. Every case below was uncovered.
"""

import anthropic
import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.llm.errors import ProviderFailure
from app.models import AuditLog, Invoice, InvoiceStatus, LlmCall
from app.services.extraction import run_extraction
from app.services.pipeline import process_invoice_pipeline
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import upload

pytestmark = pytest.mark.integration


def raise_provider(exc: BaseException):
    async def fail(*_: object, **__: object) -> None:
        raise exc

    return fail


async def invoice_of(invoice_id) -> Invoice:
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        session.expunge(invoice)
        return invoice


async def ledger_rows(invoice_id) -> list[LlmCall]:
    async with get_sessionmaker()() as session:
        return list(
            (await session.scalars(select(LlmCall).where(LlmCall.invoice_id == invoice_id))).all()
        )


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (anthropic.APITimeoutError(request=httpx.Request("POST", "http://x")), "timeout"),
        (
            anthropic.RateLimitError(
                "429",
                response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
                body=None,
            ),
            "rate_limited",
        ),
    ],
)
async def test_transient_failure_is_recorded_and_left_for_the_cron(
    client: httpx.AsyncClient,
    full_access_key: str,
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    reason: str,
) -> None:
    """A provider's bad ten minutes must not become a permanent extraction_failed on
    a perfectly readable document — but the attempt has to be on the record."""
    invoice_id = await upload(client, full_access_key, f"transient-{reason}", minimal_pdf(reason))
    monkeypatch.setattr("app.llm.fake.FakeLLM.parse_document", raise_provider(exc))

    with pytest.raises(ProviderFailure) as caught:
        await process_invoice_pipeline(invoice_id)

    assert caught.value.reason == reason
    assert caught.value.transient is True

    invoice = await invoice_of(invoice_id)
    assert invoice.status is InvoiceStatus.EXTRACTING  # resumable; the cron owns it

    # The attempt is on the ledger even though the transaction that raised unwound.
    rows = await ledger_rows(invoice_id)
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].model == "fake-llm"

    async with get_sessionmaker()() as session:
        payloads = (
            await session.scalars(
                select(AuditLog.payload).where(
                    AuditLog.invoice_id == invoice_id,
                    AuditLog.event_type == "extraction_attempt",
                )
            )
        ).all()
    assert [p["provider_failure"] for p in payloads] == [reason]


async def test_terminal_failure_fails_the_invoice_immediately(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused key answers the same way every time. Waiting fifteen minutes to ask
    again spends the retry budget to reproduce it."""
    invoice_id = await upload(client, full_access_key, "terminal-auth", minimal_pdf("authfail"))
    monkeypatch.setattr(
        "app.llm.fake.FakeLLM.parse_document",
        raise_provider(
            anthropic.AuthenticationError(
                "401",
                response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
                body=None,
            )
        ),
    )

    await process_invoice_pipeline(invoice_id)

    invoice = await invoice_of(invoice_id)
    assert invoice.status is InvoiceStatus.EXTRACTION_FAILED
    assert "401" in (invoice.error_detail or "")
    assert len(await ledger_rows(invoice_id)) == 1


async def test_every_failed_attempt_reaches_the_ledger(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cost_ledger promises a row per API attempt. That stopped being true exactly
    when it mattered: a provider outage produced spend with no record of it."""
    invoice_id = await upload(client, full_access_key, "repeat-fail", minimal_pdf("repeatfail"))
    monkeypatch.setattr(
        "app.llm.fake.FakeLLM.parse_document",
        raise_provider(anthropic.APITimeoutError(request=httpx.Request("POST", "http://x"))),
    )

    for _ in range(3):
        with pytest.raises(ProviderFailure):
            await run_extraction_once(invoice_id)

    assert len(await ledger_rows(invoice_id)) == 3


async def run_extraction_once(invoice_id) -> None:
    from app.llm.factory import build_llm_client

    settings = get_settings()
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        await session.refresh(invoice, attribute_names=["file"])
        llm = build_llm_client(settings, model=settings.extraction_model)
        await run_extraction(session, invoice, llm=llm, settings=settings)


async def test_matching_survives_a_provider_outage(
    client: httpx.AsyncClient, full_access_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Investigation is enrichment, not a gate — every discrepancy and check is
    already computed deterministically before the model is asked anything."""
    from tests.integration.test_matching_flow import seed_po_and_receipt

    await seed_po_and_receipt(client, full_access_key)
    invoice_id = await upload(client, full_access_key, "match-outage", minimal_pdf("matchoutage"))

    from app.llm.fake import scenario

    monkeypatch.setattr(
        "app.llm.fake.FakeLLM.tool_turn",
        raise_provider(anthropic.APITimeoutError(request=httpx.Request("POST", "http://x"))),
    )
    with scenario("extract-po-pricedrift"):
        await process_invoice_pipeline(invoice_id)

    invoice = await invoice_of(invoice_id)
    # Reached a real verdict on deterministic evidence rather than stalling.
    assert invoice.status is InvoiceStatus.EXCEPTION
    assert invoice.exception_reason == "matching:price_mismatch"

    async with get_sessionmaker()() as session:
        matching_calls = await session.scalar(
            select(func.count())
            .select_from(LlmCall)
            .where(LlmCall.invoice_id == invoice_id, LlmCall.purpose == "matching")
        )
    assert matching_calls == 0  # the turn never completed, so nothing is billed
