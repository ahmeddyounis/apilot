"""Accounting-format exports of approved invoices."""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import AuthContext
from app.models import Invoice, InvoiceStatus
from app.services.state_machine import transition

CSV_COLUMNS = [
    "invoice_id",
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "po_number",
    "line_no",
    "description",
    "quantity",
    "unit_price",
    "line_amount",
    "subtotal",
    "tax_amount",
    "total",
    "approved_at",
]

# Pragmatic QuickBooks Online Bill-import CSV shape.
QBO_COLUMNS = [
    "BillNo",
    "Supplier",
    "BillDate",
    "DueDate",
    "Terms",
    "Location",
    "Memo",
    "Account",
    "LineDescription",
    "LineAmount",
    "LineTaxAmount",
]


# Rows fetched per round trip while streaming. Large enough that the trip count
# stays sane, small enough that peak memory does not track the corpus.
_STREAM_BATCH = 500


@dataclass(frozen=True)
class ExportResult:
    filename: str
    content: str
    invoice_ids: list[UUID]


# Leading =, +, -, @ (and the tab/CR variants Excel also honours) make a cell a live
# formula when the file is opened in Excel, Sheets or QuickBooks. vendor_name and
# the line descriptions come off the invoice document, so the vendor writes them;
# a reviewer can set them too. Prefixing with an apostrophe is the standard defence
# and keeps the visible text intact.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: object) -> object:
    if isinstance(value, str) and value.startswith(_FORMULA_LEAD):
        return "'" + value
    return value


async def build_export(
    session: AsyncSession,
    *,
    export_format: str,
    since: datetime | None,
    until: datetime | None,
    include_exported: bool,
) -> ExportResult:
    statuses = [InvoiceStatus.APPROVED]
    if include_exported:
        statuses.append(InvoiceStatus.EXPORTED)
    conditions: list[ColumnElement[bool]] = [Invoice.status.in_(statuses)]
    if since is not None:
        conditions.append(Invoice.decided_at >= since)
    if until is not None:
        conditions.append(Invoice.decided_at <= until)

    # yield_per streams the result in batches instead of materialising every
    # approved invoice — with its extraction, validation_results and fraud_flags
    # JSONB, plus all its line items — into memory at once. `since`/`until` are
    # optional, so a bare request means "the whole history"; that is the request an
    # accounting integration makes on its first run, and it was the one thing here
    # that could take the container down.
    invoices = await session.stream_scalars(
        select(Invoice)
        .where(*conditions)
        .options(selectinload(Invoice.line_items))
        .order_by(Invoice.decided_at.asc())
        .execution_options(yield_per=_STREAM_BATCH)
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    exported_ids: list[UUID] = []
    if export_format == "quickbooks":
        writer.writerow(QBO_COLUMNS)
        async for invoice in invoices:
            if invoice.line_items:
                exported_ids.append(invoice.id)
            for line in invoice.line_items:
                writer.writerow(
                    [
                        _safe(invoice.invoice_number),
                        _safe(invoice.vendor_name),
                        invoice.invoice_date,
                        invoice.due_date or "",
                        "",
                        "",
                        f"PO {invoice.po_number}" if invoice.po_number else "",
                        "Accounts Payable",
                        _safe(line.description),
                        f"{line.amount}",
                        "",
                    ]
                )
    else:
        writer.writerow(CSV_COLUMNS)
        async for invoice in invoices:
            if invoice.line_items:
                exported_ids.append(invoice.id)
            for line in invoice.line_items:
                writer.writerow(
                    [
                        invoice.id,
                        _safe(invoice.vendor_name),
                        _safe(invoice.invoice_number),
                        invoice.invoice_date,
                        invoice.due_date or "",
                        invoice.currency,
                        _safe(invoice.po_number or ""),
                        line.line_no,
                        _safe(line.description),
                        line.quantity,
                        line.unit_price,
                        line.amount,
                        invoice.subtotal,
                        invoice.tax_amount,
                        invoice.total,
                        invoice.decided_at.isoformat() if invoice.decided_at else "",
                    ]
                )

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return ExportResult(
        filename=f"apilot_export_{export_format}_{stamp}.csv",
        content=buffer.getvalue(),
        # Only invoices that actually produced rows. An approved invoice with no
        # line items contributes nothing to the file, and marking it exported
        # would record money as delivered to accounting that was never in it.
        invoice_ids=exported_ids,
    )


_MARK_CHUNK = 500


async def mark_invoices_exported(
    session: AsyncSession, invoice_ids: list[UUID], *, auth: AuthContext
) -> int:
    """Mark a batch delivered, in chunks, one audit row per invoice.

    The per-invoice loop cost three round trips each — a SELECT, the state machine's
    own SELECT ... FOR UPDATE, and an audit INSERT — inside a single transaction that
    held a row lock on every one of them for the length of the HTTP request. Ten
    thousand invoices meant thirty thousand round trips and ten thousand locks.

    Chunking rather than one bulk UPDATE, deliberately: `transition` is the only
    writer of `invoices.status`, and it is what validates the edge and writes the
    audit row atomically with the change. A bulk UPDATE would be faster still and
    would bypass both — and an export that silently moved an invoice out of a status
    the state machine forbids is precisely the kind of thing the choke point exists
    to prevent. So the round trips stay, the transaction does not.
    """
    marked = 0
    for start in range(0, len(invoice_ids), _MARK_CHUNK):
        chunk = invoice_ids[start : start + _MARK_CHUNK]
        invoices = (
            await session.scalars(
                select(Invoice).where(
                    Invoice.id.in_(chunk),
                    Invoice.status == InvoiceStatus.APPROVED,
                )
            )
        ).all()
        for invoice in invoices:
            await transition(session, invoice, InvoiceStatus.EXPORTED, actor=auth.actor)
            marked += 1
        await session.commit()
    return marked
