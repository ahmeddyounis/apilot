"""PO and receiving-record ingestion (seed/demo data path)."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import AuthContext
from app.api.errors import ConflictError, NotFoundError
from app.core.money import quantize_amount
from app.core.normalize import normalize_vendor_name
from app.models import PoLineItem, PurchaseOrder, Receipt, ReceiptLineItem, Vendor
from app.schemas.po import POCreateIn, ReceiptCreateIn
from app.services import audit


async def get_or_create_vendor(session: AsyncSession, name: str) -> Vendor:
    normalized = normalize_vendor_name(name)
    vendor = await session.scalar(select(Vendor).where(Vendor.normalized_name == normalized))
    if vendor is None:
        vendor = Vendor(name=name, normalized_name=normalized)
        session.add(vendor)
        await session.flush()
    return vendor


async def create_purchase_order(
    session: AsyncSession, payload: POCreateIn, *, auth: AuthContext
) -> PurchaseOrder:
    existing = await session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.po_number == payload.po_number)
    )
    if existing is not None:
        raise ConflictError(f"Purchase order '{payload.po_number}' already exists")

    vendor = await get_or_create_vendor(session, payload.vendor_name)
    po = PurchaseOrder(
        po_number=payload.po_number,
        vendor_id=vendor.id,
        status=payload.status,
        currency=payload.currency,
        total=Decimal(0),
        order_date=payload.order_date,
    )
    session.add(po)
    await session.flush()

    total = Decimal(0)
    for index, line in enumerate(payload.line_items, start=1):
        amount = quantize_amount(line.quantity * line.unit_price, payload.currency)
        total += amount
        session.add(
            PoLineItem(
                po_id=po.id,
                line_no=line.line_no or index,
                sku=line.sku,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=amount,
            )
        )
    po.total = quantize_amount(total, payload.currency)

    audit.record(
        session,
        entity_type="purchase_order",
        entity_id=po.id,
        event_type="po_created",
        actor=auth.actor,
        payload={"po_number": po.po_number, "total": str(po.total)},
    )
    await session.commit()
    return po


async def create_receipt(
    session: AsyncSession, payload: ReceiptCreateIn, *, auth: AuthContext
) -> Receipt:
    po = await session.scalar(
        select(PurchaseOrder)
        .where(PurchaseOrder.po_number == payload.po_number)
        .options(selectinload(PurchaseOrder.line_items))
    )
    if po is None:
        raise NotFoundError(f"Purchase order '{payload.po_number}' not found")

    valid_line_nos = {line.line_no for line in po.line_items}
    unknown = [
        line.po_line_no for line in payload.line_items if line.po_line_no not in valid_line_nos
    ]
    if unknown:
        raise ConflictError(
            f"Receipt references unknown PO line number(s): {sorted(set(unknown))}",
            code="unknown_po_line",
        )

    receipt = Receipt(
        receipt_number=payload.receipt_number,
        po_id=po.id,
        received_date=payload.received_date,
    )
    session.add(receipt)
    await session.flush()
    for line in payload.line_items:
        session.add(
            ReceiptLineItem(
                receipt_id=receipt.id,
                po_line_no=line.po_line_no,
                quantity_received=line.quantity_received,
            )
        )
    audit.record(
        session,
        entity_type="receipt",
        entity_id=receipt.id,
        event_type="receipt_created",
        actor=auth.actor,
        payload={"receipt_number": receipt.receipt_number, "po_number": po.po_number},
    )
    await session.commit()
    return receipt
