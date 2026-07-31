"""Domain extraction model (Decimal money) + wire→domain coercion + arithmetic checks.

Design note — why arithmetic checks are functions, not model validators: a
faithfully-extracted document whose *printed* numbers don't add up must still be
persistable so the validation engine can route it to human review. Cross-field
arithmetic failures therefore feed the retry-with-feedback loop (they usually mean a
misread digit) but never make the extraction unrepresentable. Schema/coercion
failures — unparseable amounts, bad dates — are genuine model-output defects and do
gate construction.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from app.core.money import amount_tolerance, quantize_amount
from app.schemas.extraction_wire import InvoiceExtractionWire


class LineItem(BaseModel):
    description: str = Field(min_length=1)
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


class InvoiceExtraction(BaseModel):
    vendor_name: str = Field(min_length=1)
    vendor_tax_id: str | None
    invoice_number: str = Field(min_length=1)
    invoice_date: date
    due_date: date | None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    po_number: str | None
    line_items: list[LineItem]
    subtotal: Decimal
    tax_amount: Decimal
    total: Decimal
    payment_details_present: bool
    # Wire-only passthrough for fraud fingerprinting; dropped before persistence.
    payment_details_raw: str | None = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    source_page_refs: dict[str, int] = Field(default_factory=dict)


def wire_to_domain(wire: InvoiceExtractionWire) -> tuple[InvoiceExtraction | None, list[str]]:
    """Coerce the all-strings wire form into the Decimal/date domain model.

    Returns (extraction, errors); errors are collected exhaustively so the
    retry-with-feedback prompt can list everything wrong at once.
    """
    payload = wire.model_dump()
    payload["currency"] = (payload.get("currency") or "").strip().upper()
    payload["field_confidence"] = wire.field_confidence.model_dump(exclude_none=True)
    payload["source_page_refs"] = wire.source_page_refs.model_dump(exclude_none=True)
    try:
        return InvoiceExtraction.model_validate(payload), []
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        return None, errors


def arithmetic_findings(extraction: InvoiceExtraction) -> list[str]:
    """Cross-field arithmetic inconsistencies, in human-readable form."""
    findings: list[str] = []
    currency = extraction.currency
    tolerance = amount_tolerance(currency)

    for index, line in enumerate(extraction.line_items, start=1):
        expected = quantize_amount(line.quantity * line.unit_price, currency)
        if abs(expected - line.amount) > tolerance:
            findings.append(
                f"line {index} ('{line.description[:40]}'): quantity {line.quantity} x "
                f"unit_price {line.unit_price} = {expected}, but amount is {line.amount}"
            )

    if extraction.line_items:
        line_sum = quantize_amount(
            sum((line.amount for line in extraction.line_items), Decimal(0)), currency
        )
        sum_tolerance = tolerance * max(1, len(extraction.line_items))
        if abs(line_sum - extraction.subtotal) > sum_tolerance:
            findings.append(
                f"sum of line amounts is {line_sum}, but subtotal is {extraction.subtotal}"
            )

    expected_total = quantize_amount(extraction.subtotal + extraction.tax_amount, currency)
    if abs(expected_total - extraction.total) > tolerance:
        findings.append(
            f"subtotal {extraction.subtotal} + tax {extraction.tax_amount} = "
            f"{expected_total}, but total is {extraction.total}"
        )
    return findings
