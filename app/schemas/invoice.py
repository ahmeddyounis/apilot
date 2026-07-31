"""Invoice API response models."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models import InvoiceStatus
from app.schemas.common import DecimalOut


class InvoiceAcceptedOut(BaseModel):
    invoice_id: UUID
    status: InvoiceStatus


class FileInfoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sha256: str
    mime: str
    size_bytes: int
    original_filename: str | None


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line_no: int
    description: str
    quantity: DecimalOut
    unit_price: DecimalOut
    amount: DecimalOut
    sku: str | None


class InvoiceDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: InvoiceStatus
    file: FileInfoOut

    vendor_id: UUID | None
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    due_date: date | None
    currency: str | None
    po_number: str | None
    subtotal: DecimalOut | None
    tax_amount: DecimalOut | None
    total: DecimalOut | None
    payment_details_present: bool | None
    line_items: list[LineItemOut]

    extraction_model: str | None
    extraction_mode: str | None
    extraction_attempts: int
    field_confidence: dict[str, Any] | None
    source_page_refs: dict[str, Any] | None

    validation_results: dict[str, Any] | None
    fraud_flags: list[dict[str, Any]] | None
    risk_score: DecimalOut | None
    exception_reason: str | None
    hold_reason: str | None
    error_detail: str | None

    received_at: datetime
    extracted_at: datetime | None
    decided_at: datetime | None
    exported_at: datetime | None
