"""Purchase-order and receipt ingestion schemas."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import DecimalOut


class POLineIn(BaseModel):
    line_no: int | None = Field(default=None, ge=1)
    sku: str | None = None
    description: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class POCreateIn(BaseModel):
    po_number: str = Field(min_length=1, max_length=64)
    vendor_name: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    order_date: date
    status: Literal["open", "closed"] = "open"
    line_items: list[POLineIn] = Field(min_length=1)


class POCreatedOut(BaseModel):
    id: UUID
    po_number: str
    vendor_id: UUID
    total: DecimalOut
    currency: str


class ReceiptLineIn(BaseModel):
    po_line_no: int = Field(ge=1)
    quantity_received: Decimal = Field(gt=0)


class ReceiptCreateIn(BaseModel):
    receipt_number: str = Field(min_length=1, max_length=64)
    po_number: str = Field(min_length=1)
    received_date: date
    line_items: list[ReceiptLineIn] = Field(min_length=1)


class ReceiptCreatedOut(BaseModel):
    id: UUID
    receipt_number: str
    po_id: UUID
