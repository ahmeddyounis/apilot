"""Review endpoint schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import InvoiceStatus


class CorrectionIn(BaseModel):
    field_path: str = Field(
        min_length=1,
        description="e.g. 'total', 'invoice_date', 'line_items[2].quantity'",
    )
    new_value: Any


class ReviewIn(BaseModel):
    action: Literal["approve", "reject", "correct"]
    note: str | None = Field(default=None, max_length=2000)
    corrections: list[CorrectionIn] | None = None
    rerun_matching: bool = False


class ReviewOut(BaseModel):
    invoice_id: str
    action: str
    status: InvoiceStatus
    corrections_applied: int = 0
    validation_passed: bool | None = None
