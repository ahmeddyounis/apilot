"""Invoice ingestion and retrieval."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import AuthContext
from app.api.errors import ConflictError, NotFoundError
from app.core.config import Settings
from app.core.redis import get_arq_pool
from app.models import IdempotencyKey, Invoice, InvoiceFile, InvoiceStatus
from app.services import audit
from app.services.idempotency import IdempotencyVerdict, evaluate_existing
from app.services.storage import StoredFile, store_upload

logger = structlog.get_logger(__name__)

UPLOAD_ENDPOINT = "POST /api/v1/invoices"


@dataclass(frozen=True)
class IngestResult:
    invoice_id: UUID
    status: InvoiceStatus
    replayed: bool


async def ingest_upload(
    session: AsyncSession,
    *,
    settings: Settings,
    auth: AuthContext,
    file: UploadFile,
    idempotency_key: str,
) -> IngestResult:
    stored = await store_upload(
        file,
        storage_dir=settings.storage_dir,
        max_bytes=settings.max_upload_bytes,
        allowed_mime=settings.allowed_mime_types,
    )

    existing = await _existing_idempotency_row(session, auth.key_id, idempotency_key)
    if existing is not None:
        outcome = await _apply_existing(session, existing, stored.sha256)
        if outcome is not None:
            return outcome

    try:
        invoice = await _register_invoice(
            session,
            settings=settings,
            auth=auth,
            stored=stored,
            idempotency_key=idempotency_key,
        )
    except IntegrityError:
        # Concurrent request won the idempotency-key insert: adopt its outcome.
        await session.rollback()
        winner = await _existing_idempotency_row(session, auth.key_id, idempotency_key)
        if winner is None:  # pragma: no cover — the race loser must find a winner
            raise
        outcome = await _apply_existing(session, winner, stored.sha256)
        if outcome is None:  # pragma: no cover — winner row cannot be expired already
            raise ConflictError("Idempotency conflict; retry the request") from None
        return outcome

    await _enqueue_pipeline(invoice.id)
    return IngestResult(invoice_id=invoice.id, status=invoice.status, replayed=False)


async def _existing_idempotency_row(
    session: AsyncSession, api_key_id: UUID, key: str
) -> IdempotencyKey | None:
    return await session.get(IdempotencyKey, (api_key_id, UPLOAD_ENDPOINT, key))


async def _apply_existing(
    session: AsyncSession, row: IdempotencyKey, content_sha256: str
) -> IngestResult | None:
    """Return the replay outcome, raise on conflict, or None when the row expired."""
    verdict = evaluate_existing(row, content_sha256=content_sha256, now=datetime.now(UTC))
    if verdict is IdempotencyVerdict.REPLAY:
        invoice = await session.get(Invoice, row.invoice_id)
        if invoice is None:  # pragma: no cover — FK guarantees existence
            raise NotFoundError("Invoice for idempotency key no longer exists")
        return IngestResult(invoice_id=invoice.id, status=invoice.status, replayed=True)
    if verdict is IdempotencyVerdict.CONFLICT:
        raise ConflictError(
            "This Idempotency-Key was already used with a different file",
            code="idempotency_key_reuse",
        )
    await session.delete(row)
    await session.flush()
    return None


async def _register_invoice(
    session: AsyncSession,
    *,
    settings: Settings,
    auth: AuthContext,
    stored: StoredFile,
    idempotency_key: str,
) -> Invoice:
    file_row = await session.scalar(select(InvoiceFile).where(InvoiceFile.sha256 == stored.sha256))
    if file_row is None:
        file_row = InvoiceFile(
            sha256=stored.sha256,
            mime=stored.mime,
            size_bytes=stored.size_bytes,
            storage_path=stored.storage_path,
            original_filename=stored.original_filename,
        )
        session.add(file_row)
        await session.flush()

    invoice = Invoice(status=InvoiceStatus.RECEIVED, file_id=file_row.id)
    session.add(invoice)
    await session.flush()

    session.add(
        IdempotencyKey(
            api_key_id=auth.key_id,
            endpoint=UPLOAD_ENDPOINT,
            key=idempotency_key,
            content_sha256=stored.sha256,
            invoice_id=invoice.id,
            response_status=202,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.idempotency_ttl_hours),
        )
    )
    audit.record(
        session,
        entity_type="invoice",
        entity_id=invoice.id,
        invoice_id=invoice.id,
        event_type="upload",
        actor=auth.actor,
        payload={
            "sha256": stored.sha256,
            "mime": stored.mime,
            "size_bytes": stored.size_bytes,
            "original_filename": stored.original_filename,
        },
    )
    await session.commit()
    return invoice


async def _enqueue_pipeline(invoice_id: UUID) -> None:
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job("process_invoice", str(invoice_id), _job_id=f"pipeline:{invoice_id}")
    except Exception:
        # The invoice stays in `received`; the stale-job cron will requeue it.
        logger.exception("pipeline_enqueue_failed", invoice_id=str(invoice_id))


async def get_invoice_detail(session: AsyncSession, invoice_id: UUID) -> Invoice:
    invoice = await session.scalar(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.line_items), selectinload(Invoice.file))
    )
    if invoice is None:
        raise NotFoundError(f"Invoice {invoice_id} not found")
    return invoice
