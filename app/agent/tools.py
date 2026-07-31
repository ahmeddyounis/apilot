"""Read-only data access for the matching workflow.

Used two ways: called directly by deterministic graph nodes, and exposed to the
LLM as tools during the bounded investigation loop.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.state import PoData, PoLineData
from app.models import (
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    PurchaseOrder,
    Receipt,
)


def _po_data(po: PurchaseOrder) -> PoData:
    return PoData(
        id=po.id,
        po_number=po.po_number,
        vendor_id=po.vendor_id,
        currency=po.currency,
        total=po.total,
        order_date=po.order_date.isoformat(),
        status=po.status,
        lines=[
            PoLineData(
                index=index,
                line_no=line.line_no,
                sku=line.sku,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
            )
            for index, line in enumerate(po.line_items)
        ],
    )


async def lookup_po(session: AsyncSession, po_number: str) -> PoData | None:
    po = await session.scalar(
        select(PurchaseOrder)
        .where(PurchaseOrder.po_number == po_number.strip())
        .options(selectinload(PurchaseOrder.line_items))
    )
    return _po_data(po) if po else None


async def find_po_candidates(
    session: AsyncSession,
    *,
    vendor_id: UUID,
    total: Decimal,
    invoice_date: date,
    total_tolerance_pct: Decimal = Decimal("0.02"),
    window_days: int = 30,
) -> list[PoData]:
    """Bounded fallback when the invoice carries no usable PO number: same vendor,
    total within ±2%, ordered within 30 days before the invoice."""
    margin = total * total_tolerance_pct
    pos = (
        await session.scalars(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.vendor_id == vendor_id,
                PurchaseOrder.total >= total - margin,
                PurchaseOrder.total <= total + margin,
                PurchaseOrder.order_date >= invoice_date - timedelta(days=window_days),
                PurchaseOrder.order_date <= invoice_date,
            )
            .options(selectinload(PurchaseOrder.line_items))
        )
    ).all()
    return [_po_data(po) for po in pos]


async def lookup_receipts(session: AsyncSession, po_id: UUID) -> dict[int, Decimal]:
    """Total received quantity per PO line_no across all receipts for the PO."""
    rows = (
        await session.scalars(
            select(Receipt).where(Receipt.po_id == po_id).options(selectinload(Receipt.line_items))
        )
    ).all()
    received: dict[int, Decimal] = {}
    for receipt in rows:
        for line in receipt.line_items:
            received[line.po_line_no] = received.get(line.po_line_no, Decimal(0)) + Decimal(
                line.quantity_received
            )
    return received


async def vendor_price_history(
    session: AsyncSession,
    *,
    vendor_id: UUID,
    description: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recent unit prices this vendor billed for similar line descriptions."""
    rows = (
        await session.execute(
            select(
                Invoice.invoice_date,
                Invoice.invoice_number,
                InvoiceLineItem.description,
                InvoiceLineItem.unit_price,
            )
            .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
            .where(
                Invoice.vendor_id == vendor_id,
                Invoice.status.in_(
                    (InvoiceStatus.APPROVED, InvoiceStatus.EXPORTED, InvoiceStatus.EXTRACTED)
                ),
            )
            .order_by(Invoice.invoice_date.desc())
            .limit(200)
        )
    ).all()

    scored = sorted(
        ((fuzz.token_set_ratio(description, row.description), row) for row in rows),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [
        {
            "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
            "invoice_number": row.invoice_number,
            "description": row.description,
            "unit_price": str(row.unit_price),
            "similarity": round(score / 100, 2),
        }
        for score, row in scored[:limit]
        if score >= 60
    ]


# --- tool schemas exposed to the LLM during investigation ---------------------------

INVESTIGATION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_po",
        "description": (
            "Fetch a purchase order by its PO number, including all its line items "
            "(sku, description, quantity, unit price). Use when you need to inspect "
            "a PO other than the one already provided."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"po_number": {"type": "string"}},
            "required": ["po_number"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_receipts",
        "description": (
            "Fetch total received quantities per PO line for a purchase order "
            "number. Use to check whether goods were partially delivered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"po_number": {"type": "string"}},
            "required": ["po_number"],
            "additionalProperties": False,
        },
    },
    {
        "name": "vendor_price_history",
        "description": (
            "Recent unit prices this invoice's vendor billed for similar item "
            "descriptions. Use to judge whether a price drift looks like a "
            "legitimate increase or an outlier."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["description"],
            "additionalProperties": False,
        },
    },
]
