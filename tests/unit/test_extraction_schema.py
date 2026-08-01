from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.llm.types import describe_schema_mismatch
from app.schemas.extraction import arithmetic_findings, wire_to_domain
from app.schemas.extraction_wire import InvoiceExtractionWire
from app.services.extraction import parse_json_loose

pytestmark = pytest.mark.unit


def wire_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "vendor_name": "ACME Office Supply Co",
        "vendor_tax_id": None,
        "invoice_number": "INV-1",
        "invoice_date": "2026-07-01",
        "due_date": "2026-07-31",
        "currency": "USD",
        "po_number": None,
        "line_items": [
            {"description": "Widget", "quantity": "10", "unit_price": "47.00", "amount": "470.00"}
        ],
        "subtotal": "470.00",
        "tax_amount": "37.60",
        "total": "507.60",
        "payment_details_present": False,
        "payment_details_raw": None,
        "field_confidence": {"vendor_name": 0.9},
        "source_page_refs": {"vendor_name": 1},
    }
    payload.update(overrides)
    return payload


def make_wire(**overrides: Any) -> InvoiceExtractionWire:
    return InvoiceExtractionWire.model_validate(wire_payload(**overrides))


def test_wire_to_domain_happy_path() -> None:
    domain, errors = wire_to_domain(make_wire())
    assert errors == []
    assert domain is not None
    assert domain.total == Decimal("507.60")
    assert domain.invoice_date.isoformat() == "2026-07-01"
    assert domain.field_confidence == {"vendor_name": 0.9}
    assert arithmetic_findings(domain) == []


def test_wire_to_domain_accumulates_all_errors() -> None:
    wire = make_wire(
        invoice_date="07/01/2026",
        line_items=[
            {"description": "W", "quantity": "ten", "unit_price": "47.00", "amount": "470.00"}
        ],
    )
    domain, errors = wire_to_domain(wire)
    assert domain is None
    assert len(errors) == 2
    joined = " | ".join(errors)
    assert "invoice_date" in joined
    assert "quantity" in joined


def test_currency_is_uppercased_before_validation() -> None:
    domain, errors = wire_to_domain(make_wire(currency="usd"))
    assert errors == []
    assert domain is not None and domain.currency == "USD"


def test_arithmetic_findings_line_math() -> None:
    domain, _ = wire_to_domain(
        make_wire(
            line_items=[
                {"description": "W", "quantity": "10", "unit_price": "47.00", "amount": "480.00"}
            ],
            subtotal="480.00",
            tax_amount="0",
            total="480.00",
        )
    )
    assert domain is not None
    findings = arithmetic_findings(domain)
    assert len(findings) == 1
    assert "line 1" in findings[0]


def test_arithmetic_findings_subtotal_and_total() -> None:
    domain, _ = wire_to_domain(make_wire(subtotal="500.00", total="600.00"))
    assert domain is not None
    findings = arithmetic_findings(domain)
    assert len(findings) == 2  # line-sum vs subtotal, subtotal+tax vs total


def test_arithmetic_tolerance_is_currency_aware() -> None:
    # 1 JPY off on a zero-decimal currency is within one-unit tolerance.
    domain, errors = wire_to_domain(
        make_wire(
            currency="JPY",
            line_items=[
                {"description": "W", "quantity": "3", "unit_price": "333", "amount": "1000"}
            ],
            subtotal="1000",
            tax_amount="0",
            total="1000",
        )
    )
    assert errors == []
    assert domain is not None
    assert arithmetic_findings(domain) == []


def test_parse_json_loose_handles_fences_and_prose() -> None:
    text = (
        "Sure! Here is the data:\n```json\n"
        + str(__import__("json").dumps(wire_payload()))
        + "\n```\nHope that helps."
    )
    wire, error = parse_json_loose(text)
    assert error is None
    assert wire is not None and wire.vendor_name == "ACME Office Supply Co"


def test_parse_json_loose_reports_garbage() -> None:
    wire, error = parse_json_loose("no json here at all")
    assert wire is None
    assert error is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total", "1E+30"),  # past Decimal's context precision: quantize() raises
        ("total", "1e13"),  # quantizes fine, but NUMERIC(14,2) overflows on INSERT
        ("subtotal", "999999999999999"),
        ("tax_amount", "-1e20"),
    ],
)
def test_implausible_amounts_are_coercion_errors_not_crashes(field: str, value: str) -> None:
    """A misread figure must route through retry-with-feedback, not raise.

    Both limits sit outside the extraction service's only handler, so before the
    bound an oversized number escaped as InvalidOperation (from quantize) or
    DataError (from the INSERT) and left the invoice stranded in `extracting`
    while arq re-ran the task — a real LLM call per retry, same crash each time.
    """
    domain, errors = wire_to_domain(make_wire(**{field: value}))

    assert domain is None
    assert any(field in err and "implausibly large" in err for err in errors), errors


def test_bounded_amounts_still_reach_the_arithmetic_checks() -> None:
    """The bound must not eat legitimate large invoices, or fine-grained prices."""
    domain, errors = wire_to_domain(
        make_wire(
            line_items=[
                {
                    "description": "Bulk resin",
                    "quantity": "1000000",
                    "unit_price": "999.9999",
                    "amount": "999999900.00",
                }
            ],
            subtotal="999999900.00",
            tax_amount="0.00",
            total="999999900.00",
        )
    )

    assert errors == []
    assert domain is not None
    assert arithmetic_findings(domain) == []


def test_schema_mismatch_never_echoes_the_offending_payload() -> None:
    """A schema failure must name fields, not quote them.

    Pydantic's errors() entries carry an `input` key holding what failed to validate,
    and for a root-level mismatch that is the entire model payload. This string is
    stored on invoices.error_detail and served to any key with `read`, so including
    `input` published the extraction verbatim — including payment_details_raw, the
    vendor's raw bank details, which _persist otherwise takes deliberate care to drop
    before writing the immutable extraction JSONB.
    """
    remittance = "Remit to IBAN GB29 NWBK 6016 1331 9268 19"
    try:
        InvoiceExtractionWire.model_validate(
            {"payment_details_raw": remittance, "vendor_name": None}
        )
    except ValidationError as exc:
        message = describe_schema_mismatch(exc)
    else:  # pragma: no cover - the payload above is not valid
        raise AssertionError("expected a validation error")

    assert "GB29" not in message and remittance not in message
    assert "vendor_name" in message  # still says which field, and why
    assert "9268" not in message


def test_root_level_mismatch_also_stays_quiet() -> None:
    """The worst case: `input` is the whole payload, not one field's value."""
    try:
        InvoiceExtractionWire.model_validate([{"payment_details_raw": "IBAN GB29 NWBK 6016"}])
    except ValidationError as exc:
        message = describe_schema_mismatch(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a validation error")

    assert "IBAN" not in message and "GB29" not in message
