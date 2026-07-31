"""Table-driven tests for every validation rule."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.services.validation import (
    LineInput,
    RuleContext,
    ValidationInput,
    run_rules,
)

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 31)


def line(
    no: int = 1,
    description: str = "Widget",
    quantity: str = "10",
    unit_price: str = "47.00",
    amount: str = "470.00",
) -> LineInput:
    return LineInput(
        line_no=no,
        description=description,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        amount=Decimal(amount),
    )


def make_input(**overrides: Any) -> ValidationInput:
    base: dict[str, Any] = {
        "currency": "USD",
        "line_items": [line()],
        "subtotal": Decimal("470.00"),
        "tax_amount": Decimal("37.60"),
        "total": Decimal("507.60"),
        "invoice_date": date(2026, 7, 1),
        "due_date": date(2026, 7, 31),
        "field_confidence": {"total": 0.99, "vendor_name": 0.95},
    }
    base.update(overrides)
    return ValidationInput(**base)


def make_ctx(**overrides: Any) -> RuleContext:
    base: dict[str, Any] = {
        "today": TODAY,
        "supported_currencies": frozenset({"USD", "EUR", "JPY"}),
        "confidence_floor": 0.70,
        "vendor_known": True,
    }
    base.update(overrides)
    return RuleContext(**base)


def rule_ids(inputs: ValidationInput, ctx: RuleContext) -> list[str]:
    return [f.rule_id for f in run_rules(inputs, ctx).findings]


def test_clean_invoice_passes_with_no_findings() -> None:
    result = run_rules(make_input(), make_ctx())
    assert result.passed
    assert result.findings == []


def test_v001_line_math_hard_fail_and_tolerance_edge() -> None:
    bad = make_input(line_items=[line(amount="471.00")], subtotal=Decimal("471.00"))
    assert "V-001" in rule_ids(bad, make_ctx())
    # Off by exactly one cent = within one-ULP tolerance → passes.
    edge = make_input(
        line_items=[line(amount="470.01")],
        subtotal=Decimal("470.01"),
        total=Decimal("507.61"),
    )
    assert "V-001" not in rule_ids(edge, make_ctx())


def test_v002_line_sum_scales_tolerance_with_line_count() -> None:
    lines = [line(no=i, amount="100.00", quantity="1", unit_price="100.00") for i in (1, 2, 3)]
    off_by_three_cents = make_input(
        line_items=lines,
        subtotal=Decimal("299.97"),
        tax_amount=Decimal("0"),
        total=Decimal("299.97"),
    )
    assert "V-002" not in rule_ids(off_by_three_cents, make_ctx())
    off_by_more = make_input(
        line_items=lines,
        subtotal=Decimal("299.00"),
        tax_amount=Decimal("0"),
        total=Decimal("299.00"),
    )
    assert "V-002" in rule_ids(off_by_more, make_ctx())


def test_v003_total_math() -> None:
    assert "V-003" in rule_ids(make_input(total=Decimal("999.99")), make_ctx())
    assert "V-003" not in rule_ids(make_input(), make_ctx())


def test_v004_tax_rate_flag_boundary() -> None:
    at_half = make_input(tax_amount=Decimal("235.00"), total=Decimal("705.00"))
    assert "V-004" not in rule_ids(at_half, make_ctx())
    above = make_input(tax_amount=Decimal("240.00"), total=Decimal("710.00"))
    ids = rule_ids(above, make_ctx())
    assert "V-004" in ids


def test_v004_flags_tax_charged_on_no_subtotal() -> None:
    """The bypass: with subtotal 0 the implied rate is infinite, so skipping the
    rule let a wholly unsubstantiated amount pass all fourteen rules cleanly."""
    all_tax = make_input(
        line_items=[line(quantity="0", unit_price="0", amount="0")],
        subtotal=Decimal("0"),
        tax_amount=Decimal("2400.00"),
        total=Decimal("2400.00"),
    )

    result = run_rules(all_tax, make_ctx())

    assert "V-004" in [f.rule_id for f in result.findings], (
        f"an invoice that is 100% tax passed clean: {result.findings}"
    )


def test_v005_currency_allowlist() -> None:
    result = run_rules(make_input(currency="XXX"), make_ctx())
    assert "V-005" in [f.rule_id for f in result.findings]
    assert not result.passed


def test_v006_non_negative() -> None:
    negative_qty = make_input(
        line_items=[line(quantity="-1", amount="-47.00")],
        subtotal=Decimal("-47.00"),
        tax_amount=Decimal("0"),
        total=Decimal("-47.00"),
    )
    ids = rule_ids(negative_qty, make_ctx())
    assert ids.count("V-006") >= 3  # qty, line amount, subtotal, total


def test_v007_requires_line_items() -> None:
    empty = make_input(
        line_items=[], subtotal=Decimal("0"), tax_amount=Decimal("0"), total=Decimal("0")
    )
    assert "V-007" in rule_ids(empty, make_ctx())


def test_v008_invoice_date_window() -> None:
    assert "V-008" not in rule_ids(make_input(invoice_date=TODAY), make_ctx())
    tomorrow = make_input(invoice_date=date(2026, 8, 1))
    assert "V-008" not in rule_ids(tomorrow, make_ctx())  # +1 day grace
    future = make_input(invoice_date=date(2026, 8, 15), due_date=date(2026, 9, 15))
    assert "V-008" in rule_ids(future, make_ctx())
    ancient = make_input(invoice_date=date(2010, 1, 1), due_date=date(2010, 2, 1))
    assert "V-008" in rule_ids(ancient, make_ctx())


def test_v009_due_date_ordering() -> None:
    equal = make_input(due_date=date(2026, 7, 1))
    assert "V-009" not in rule_ids(equal, make_ctx())
    before = make_input(due_date=date(2026, 6, 30))
    assert "V-009" in rule_ids(before, make_ctx())
    absent = make_input(due_date=None)
    assert "V-009" not in rule_ids(absent, make_ctx())


def test_v010_invoice_before_po_flag() -> None:
    ctx = make_ctx(po_order_date=date(2026, 7, 15))
    ids = rule_ids(make_input(), ctx)  # invoice 2026-07-01 < PO 2026-07-15
    assert "V-010" in ids
    assert "V-010" not in rule_ids(make_input(), make_ctx())  # no PO date → skip


def test_v011_duplicate_lines_flag() -> None:
    dupes = make_input(
        line_items=[line(no=1), line(no=2)],
        subtotal=Decimal("940.00"),
        tax_amount=Decimal("0"),
        total=Decimal("940.00"),
    )
    result = run_rules(dupes, make_ctx())
    v011 = [f for f in result.findings if f.rule_id == "V-011"]
    assert len(v011) == 1
    assert result.passed  # flags never gate


def test_v012_unknown_vendor_flag() -> None:
    result = run_rules(make_input(), make_ctx(vendor_known=False))
    assert "V-012" in [f.rule_id for f in result.findings]
    assert result.passed


def test_v013_low_confidence_flag() -> None:
    low = make_input(field_confidence={"total": 0.4, "vendor_name": 0.95})
    result = run_rules(low, make_ctx())
    v013 = [f for f in result.findings if f.rule_id == "V-013"]
    assert len(v013) == 1
    assert "total=0.40" in v013[0].message


def test_v014_zero_total_flag() -> None:
    zero = make_input(
        line_items=[line(quantity="1", unit_price="0", amount="0.00")],
        subtotal=Decimal("0"),
        tax_amount=Decimal("0"),
        total=Decimal("0"),
    )
    ids = rule_ids(zero, make_ctx())
    assert "V-014" in ids


def test_multiple_hard_failures_are_all_reported() -> None:
    mess = make_input(
        currency="XXX",
        total=Decimal("1.00"),
        due_date=date(2026, 6, 1),
    )
    result = run_rules(mess, make_ctx())
    ids = {f.rule_id for f in result.findings if f.severity == "hard_fail"}
    assert {"V-003", "V-005", "V-009"} <= ids
    assert not result.passed


def test_jpy_zero_decimal_tolerance() -> None:
    jpy = make_input(
        currency="JPY",
        line_items=[line(quantity="3", unit_price="333", amount="1000")],
        subtotal=Decimal("1000"),
        tax_amount=Decimal("0"),
        total=Decimal("1000"),
        field_confidence={},
    )
    result = run_rules(jpy, make_ctx())
    assert result.passed
    assert "V-001" not in [f.rule_id for f in result.findings]
