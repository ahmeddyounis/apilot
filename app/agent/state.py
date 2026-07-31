"""LangGraph state for the 3-way matching workflow."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.matching import Discrepancy, MatchingChecks, MatchingVerdict


class InvoiceLineData(BaseModel):
    index: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


class PoLineData(BaseModel):
    index: int
    line_no: int
    sku: str | None
    description: str
    quantity: Decimal
    unit_price: Decimal


class PoData(BaseModel):
    id: UUID
    po_number: str
    vendor_id: UUID
    currency: str
    total: Decimal
    order_date: str
    status: str
    lines: list[PoLineData]


class AlignedPair(BaseModel):
    invoice_index: int
    po_index: int
    similarity: float
    source: str = "deterministic"  # deterministic | llm_proposed


class NodeEvent(BaseModel):
    seq: int
    node: str
    duration_ms: int
    summary: dict[str, Any] = Field(default_factory=dict)


class MatchingState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    invoice_id: UUID
    trace_id: str
    currency: str = "USD"
    total: Decimal = Decimal(0)
    vendor_id: UUID | None = None
    vendor_name: str | None = None
    invoice_number: str | None = None
    po_number_raw: str | None = None
    invoice_date: str | None = None
    fraud_flag_codes: list[str] = Field(default_factory=list)  # all codes, informational
    blocking_fraud_codes: list[str] = Field(default_factory=list)  # medium/high only

    invoice_lines: list[InvoiceLineData] = Field(default_factory=list)
    po: PoData | None = None
    po_resolution: str = "not_found"  # by_number | fuzzy | not_found | no_po_number
    received_by_po_line: dict[int, Decimal] = Field(default_factory=dict)
    # Total already billed against this PO by other invoices that reached a paying
    # state — the budget this invoice has to fit inside.
    po_billed_elsewhere: Decimal = Decimal(0)

    alignment: list[AlignedPair] = Field(default_factory=list)
    unmatched_invoice: list[int] = Field(default_factory=list)
    unmatched_po: list[int] = Field(default_factory=list)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    checks: MatchingChecks = Field(default_factory=MatchingChecks)

    needs_investigation: bool = False
    investigated: bool = False
    investigation_summary: str | None = None
    investigation_skipped: str | None = None

    events: list[NodeEvent] = Field(default_factory=list)
    verdict: MatchingVerdict | None = None
