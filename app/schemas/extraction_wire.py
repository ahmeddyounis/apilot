"""Wire-format extraction schemas — what the LLM is constrained to emit.

Two deliberate deviations from the domain shape:

* **Money and dates are strings.** JSON numbers are floats; floats are unacceptable
  for money. Pydantic coerces the strings to Decimal/date in the domain model.
* **`field_confidence` / `source_page_refs` are fixed-key models, not dicts.**
  Structured outputs require ``additionalProperties: false``, which open dicts
  cannot express. The domain model restores the spec's dict shape.

`payment_details_raw` is wire-only: the verbatim remittance text feeds the
bank-detail-change fraud fingerprint (HMAC) and is never persisted.
"""

from pydantic import BaseModel, ConfigDict


class LineItemWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    quantity: str
    unit_price: str
    amount: str


class FieldConfidenceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: float | None = None
    vendor_tax_id: float | None = None
    invoice_number: float | None = None
    invoice_date: float | None = None
    due_date: float | None = None
    currency: float | None = None
    po_number: float | None = None
    subtotal: float | None = None
    tax_amount: float | None = None
    total: float | None = None
    line_items: float | None = None
    payment_details_present: float | None = None


class SourcePageRefsWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: int | None = None
    vendor_tax_id: int | None = None
    invoice_number: int | None = None
    invoice_date: int | None = None
    due_date: int | None = None
    currency: int | None = None
    po_number: int | None = None
    subtotal: int | None = None
    tax_amount: int | None = None
    total: int | None = None
    line_items: int | None = None
    payment_details_present: int | None = None


class InvoiceExtractionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: str
    vendor_tax_id: str | None
    invoice_number: str
    invoice_date: str
    due_date: str | None
    currency: str
    po_number: str | None
    line_items: list[LineItemWire]
    subtotal: str
    tax_amount: str
    total: str
    payment_details_present: bool
    payment_details_raw: str | None
    field_confidence: FieldConfidenceWire
    source_page_refs: SourcePageRefsWire


class PageExtractionWire(BaseModel):
    """Per-page extraction for page-by-page mode; merged deterministically."""

    model_config = ConfigDict(extra="forbid")

    page_number: int
    vendor_name: str | None
    vendor_tax_id: str | None
    invoice_number: str | None
    invoice_date: str | None
    due_date: str | None
    currency: str | None
    po_number: str | None
    line_items: list[LineItemWire]
    subtotal: str | None
    tax_amount: str | None
    total: str | None
    payment_details_present: bool
    payment_details_raw: str | None
    field_confidence: FieldConfidenceWire
