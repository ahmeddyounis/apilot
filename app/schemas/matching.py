"""Matching verdict models — the spec's JSON shape, assembled by code only."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MatchingChecks(BaseModel):
    po_found: bool = False
    vendor_match: bool = False
    quantity_match: bool = False
    price_match: bool = False
    receipt_match: bool = False

    def all_pass(self) -> bool:
        return all(self.model_dump().values())


class Impact(BaseModel):
    amount: str
    currency: str


class Discrepancy(BaseModel):
    type: Literal[
        "quantity_mismatch",
        "price_mismatch",
        "receipt_shortfall",
        "unmatched_line",
        "missing_po",
        "vendor_mismatch",
        "currency_mismatch",
        "po_over_consumption",
    ]
    line: str | None = None
    invoice_qty: str | None = None
    po_qty: str | None = None
    invoice_price: str | None = None
    po_price: str | None = None
    received_qty: str | None = None
    impact: Impact | None = None
    note: str | None = None


class MatchingVerdict(BaseModel):
    invoice_id: str
    verdict: Literal["auto_approve", "exception", "reject"]
    auto_approve_eligible: bool
    checks: MatchingChecks
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    fraud_flags: list[str] = Field(default_factory=list)
    agent_summary: str
    trace_id: str
    po_number: str | None = None
    po_resolution: Literal["by_number", "fuzzy", "not_found", "no_po_number"] = "not_found"


class InvestigationOutcome(BaseModel):
    """What the LLM investigation may propose — code re-validates everything."""

    model_config = ConfigDict(extra="forbid")

    proposed_pairs: list[list[int]] = Field(default_factory=list)  # [invoice_idx, po_idx]
    summary: str = ""


class MatchingResultOut(BaseModel):
    invoice_id: str
    created_at: str
    verdict: dict[str, Any]
    trace: list[dict[str, Any]]
