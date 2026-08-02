"""Invoice aggregate: file, invoice, line items, and the status enum."""

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class InvoiceStatus(enum.StrEnum):
    RECEIVED = "received"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    EXTRACTION_FAILED = "extraction_failed"
    MATCHING = "matching"
    EXCEPTION = "exception"
    HELD = "held"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPORTED = "exported"


# Stored as VARCHAR + CHECK (never a native PG enum) so adding a status stays a
# painless migration. create_constraint must be explicit — it has defaulted to False
# since SQLAlchemy 1.4, so the comment above described a constraint that was never
# emitted. Without it Postgres accepted any string: a stray write of 'paid' was
# stored happily and then raised LookupError on every read, leaving a row the API
# couldn't return and the worker couldn't process.
invoice_status_type = Enum(
    InvoiceStatus,
    name="invoice_status",
    native_enum=False,
    create_constraint=True,
    length=32,
    values_callable=lambda e: [member.value for member in e],
)


class InvoiceFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Content-addressed upload: identical bytes reuse the same row."""

    __tablename__ = "invoice_files"

    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text)


class Invoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "invoices"

    status: Mapped[InvoiceStatus] = mapped_column(
        invoice_status_type, nullable=False, default=InvoiceStatus.RECEIVED
    )
    file_id: Mapped[UUID] = mapped_column(ForeignKey("invoice_files.id"), nullable=False)
    vendor_id: Mapped[UUID | None] = mapped_column(ForeignKey("vendors.id"))

    # Extracted header fields (typed columns; reviewer corrections update these)
    vendor_name: Mapped[str | None] = mapped_column(Text)
    invoice_number: Mapped[str | None] = mapped_column(Text)
    normalized_invoice_number: Mapped[str | None] = mapped_column(Text)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(String(3))
    po_number: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    payment_details_present: Mapped[bool | None] = mapped_column(Boolean)
    # HMAC of remittance details from the wire-only raw text (never stored itself).
    payment_fingerprint: Mapped[str | None] = mapped_column(Text)

    # Raw extraction output (immutable once written) + extraction metadata
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    extraction_model: Mapped[str | None] = mapped_column(Text)
    extraction_mode: Mapped[str | None] = mapped_column(String(16))
    # LLM calls spent on extraction (retry-with-feedback counts here).
    extraction_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Times the stale-job cron re-queued this invoice. Kept separate from
    # extraction_attempts, which counts something else entirely: an invoice that
    # legitimately used its extraction retries would otherwise arrive at the matching
    # stage with its whole requeue budget already spent, and be abandoned on the
    # first rescue tick.
    pipeline_requeues: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    field_confidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_page_refs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Stage outputs
    validation_results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    fraud_flags: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    exception_reason: Mapped[str | None] = mapped_column(Text)
    hold_reason: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)

    # Lifecycle timestamps
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    extracted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    exported_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    line_items: Mapped[list["InvoiceLineItem"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLineItem.line_no",
    )
    file: Mapped[InvoiceFile] = relationship()

    # Maintained by Postgres, never by Python. The review queue used to sort by this
    # expression inline — a CASE over two coalesced mutable columns, which no index
    # can serve — so every page recomputed it for every queued invoice.
    review_priority: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 3),
        Computed(
            "CASE WHEN COALESCE(total, 0) > 0 "
            "THEN COALESCE(risk_score, 0.5) * COALESCE(total, 0) "
            "ELSE COALESCE(risk_score, 0.5) END",
            persisted=True,
        ),
    )

    __table_args__ = (
        Index("ix_invoices_status", "status"),
        Index("ix_invoices_file_id", "file_id"),
        Index("ix_invoices_received_at", "received_at"),
        Index("ix_invoices_decided_at", "decided_at"),
        Index("ix_invoices_updated_at", "updated_at"),
        Index(
            "ix_invoices_normalized_invoice_number",
            "normalized_invoice_number",
            postgresql_where=text("normalized_invoice_number IS NOT NULL"),
        ),
        Index(
            "ix_invoices_review_queue",
            text("review_priority DESC"),
            text("received_at ASC"),
            postgresql_where=text("status IN ('exception', 'held')"),
        ),
        # Deliberately NON-unique: duplicates are held by the dedup stage with an
        # explanation, never auto-rejected by a constraint violation.
        Index(
            "ix_invoices_vendor_invoice_number",
            "vendor_id",
            "normalized_invoice_number",
        ),
    )


class InvoiceLineItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "invoice_line_items"

    invoice_id: Mapped[UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sku: Mapped[str | None] = mapped_column(Text)

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")

    __table_args__ = (UniqueConstraint("invoice_id", "line_no"),)
