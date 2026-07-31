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


@dataclass(frozen=True)
class ExportResult:
    filename: str
    content: str
    invoice_ids: list[UUID]


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

    invoices = (
        await session.scalars(
            select(Invoice)
            .where(*conditions)
            .options(selectinload(Invoice.line_items))
            .order_by(Invoice.decided_at.asc())
        )
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if export_format == "quickbooks":
        writer.writerow(QBO_COLUMNS)
        for invoice in invoices:
            for line in invoice.line_items:
                writer.writerow(
                    [
                        invoice.invoice_number,
                        invoice.vendor_name,
                        invoice.invoice_date,
                        invoice.due_date or "",
                        "",
                        "",
                        f"PO {invoice.po_number}" if invoice.po_number else "",
                        "Accounts Payable",
                        line.description,
                        f"{line.amount}",
                        "",
                    ]
                )
    else:
        writer.writerow(CSV_COLUMNS)
        for invoice in invoices:
            for line in invoice.line_items:
                writer.writerow(
                    [
                        invoice.id,
                        invoice.vendor_name,
                        invoice.invoice_number,
                        invoice.invoice_date,
                        invoice.due_date or "",
                        invoice.currency,
                        invoice.po_number or "",
                        line.line_no,
                        line.description,
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
        invoice_ids=[invoice.id for invoice in invoices],
    )


async def mark_invoices_exported(
    session: AsyncSession, invoice_ids: list[UUID], *, auth: AuthContext
) -> int:
    marked = 0
    for invoice_id in invoice_ids:
        invoice = await session.get(Invoice, invoice_id)
        if invoice is None or invoice.status is not InvoiceStatus.APPROVED:
            continue
        await transition(session, invoice, InvoiceStatus.EXPORTED, actor=auth.actor)
        marked += 1
    await session.commit()
    return marked
