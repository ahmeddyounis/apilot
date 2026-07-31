"""Review-queue response models."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models import InvoiceStatus
from app.schemas.common import DecimalOut


class ExceptionItemOut(BaseModel):
    id: UUID
    status: InvoiceStatus
    vendor_name: str | None
    invoice_number: str | None
    currency: str | None
    total: DecimalOut | None
    risk_score: DecimalOut | None
    exception_reason: str | None
    hold_reason: str | None
    failed_rules: list[str]
    fraud_flag_codes: list[str]
    received_at: datetime
    decided_at: datetime | None


class ExceptionsPageOut(BaseModel):
    items: list[ExceptionItemOut]
    total_count: int
    limit: int
    offset: int
