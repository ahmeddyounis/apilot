from typing import Any

import pytest

from app.schemas.extraction_wire import InvoiceExtractionWire, PageExtractionWire
from app.services.extraction import merge_pages

pytestmark = pytest.mark.unit


def page(number: int, **overrides: Any) -> PageExtractionWire:
    payload: dict[str, Any] = {
        "page_number": number,
        "vendor_name": None,
        "vendor_tax_id": None,
        "invoice_number": None,
        "invoice_date": None,
        "due_date": None,
        "currency": None,
        "po_number": None,
        "line_items": [],
        "subtotal": None,
        "tax_amount": None,
        "total": None,
        "payment_details_present": False,
        "payment_details_raw": None,
        "field_confidence": {},
    }
    payload.update(overrides)
    return PageExtractionWire.model_validate(payload)


LINE_A = {"description": "A", "quantity": "1", "unit_price": "10.00", "amount": "10.00"}
LINE_B = {"description": "B", "quantity": "2", "unit_price": "5.00", "amount": "10.00"}
LINE_C = {"description": "C", "quantity": "1", "unit_price": "30.00", "amount": "30.00"}


def test_header_fields_pick_highest_confidence_with_first_page_tiebreak() -> None:
    pages = [
        page(
            1,
            vendor_name="ACME Corp",
            invoice_number="INV-9",
            invoice_date="2026-01-01",
            currency="USD",
            field_confidence={"vendor_name": 0.7, "invoice_number": 0.9},
        ),
        page(
            2,
            vendor_name="ACME Corporation",
            invoice_number="INV-9",
            field_confidence={"vendor_name": 0.95, "invoice_number": 0.9},
        ),
    ]
    merged = merge_pages(pages)
    assert merged["vendor_name"] == "ACME Corporation"  # higher confidence wins
    assert merged["source_page_refs"]["vendor_name"] == 2
    assert merged["invoice_number"] == "INV-9"  # tie → earliest page
    assert merged["source_page_refs"]["invoice_number"] == 1


def test_totals_come_from_last_totals_page() -> None:
    pages = [
        page(1, subtotal="99.00", tax_amount="1.00", total="100.00"),  # partial-page totals
        page(2, line_items=[LINE_A]),
        page(3, subtotal="50.00", tax_amount="0", total="50.00"),
    ]
    merged = merge_pages(pages)
    assert merged["total"] == "50.00"
    assert merged["subtotal"] == "50.00"
    assert merged["source_page_refs"]["total"] == 3


def test_line_items_concatenate_and_drop_carried_forward_row() -> None:
    pages = [
        page(1, line_items=[LINE_A, LINE_B], field_confidence={"line_items": 0.9}),
        # Page 2 repeats page 1's last row (carried forward), then continues.
        page(2, line_items=[LINE_B, LINE_C], field_confidence={"line_items": 0.8}),
    ]
    merged = merge_pages(pages)
    descriptions = [line["description"] for line in merged["line_items"]]
    assert descriptions == ["A", "B", "C"]
    assert merged["field_confidence"]["line_items"] == 0.8  # conservative min
    assert merged["source_page_refs"]["line_items"] == 1


def test_payment_details_and_merged_wire_validates() -> None:
    pages = [
        page(
            1,
            vendor_name="V",
            invoice_number="I-1",
            invoice_date="2026-02-02",
            currency="EUR",
            line_items=[LINE_A],
            field_confidence={"vendor_name": 0.9},
        ),
        page(
            2,
            subtotal="10.00",
            tax_amount="0",
            total="10.00",
            payment_details_present=True,
            payment_details_raw="IBAN DE00 1234",
        ),
    ]
    merged = merge_pages(pages)
    assert merged["payment_details_present"] is True
    assert merged["payment_details_raw"] == "IBAN DE00 1234"
    wire = InvoiceExtractionWire.model_validate(merged)
    assert wire.total == "10.00"
