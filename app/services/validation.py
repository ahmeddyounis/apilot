"""Deterministic validation rules — zero LLM calls, exhaustively unit-tested.

"LLMs extract, code decides": every rule is a pure function over extracted values
plus a small context. ``hard_fail`` findings route the invoice to the human
exception queue; ``flag`` findings annotate without gating.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.money import amount_tolerance, quantize_amount
from app.models import Invoice, InvoiceStatus
from app.services import audit
from app.services.state_machine import transition

Severity = Literal["hard_fail", "flag"]


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    field: str | None = None
    message: str
    expected: str | None = None
    actual: str | None = None


class ValidationResult(BaseModel):
    findings: list[Finding]

    @property
    def passed(self) -> bool:
        return not any(f.severity == "hard_fail" for f in self.findings)

    def to_jsonb(self) -> dict:
        return {
            "passed": self.passed,
            "findings": [f.model_dump() for f in self.findings],
        }


@dataclass(frozen=True)
class LineInput:
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class ValidationInput:
    currency: str
    line_items: list[LineInput]
    subtotal: Decimal
    tax_amount: Decimal
    total: Decimal
    invoice_date: date
    due_date: date | None
    field_confidence: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleContext:
    today: date
    supported_currencies: frozenset[str]
    confidence_floor: float
    vendor_known: bool
    # Provided once matching data exists (P4); rule V-010 skips when None.
    po_order_date: date | None = None


Rule = Callable[[ValidationInput, RuleContext], list[Finding]]


def rule_v001_line_math(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    tolerance = amount_tolerance(inputs.currency)
    findings = []
    for line in inputs.line_items:
        expected = quantize_amount(line.quantity * line.unit_price, inputs.currency)
        if abs(expected - line.amount) > tolerance:
            findings.append(
                Finding(
                    rule_id="V-001",
                    severity="hard_fail",
                    field=f"line_items[{line.line_no}].amount",
                    message=(f"Line {line.line_no}: quantity x unit_price does not equal amount"),
                    expected=str(expected),
                    actual=str(line.amount),
                )
            )
    return findings


def rule_v002_line_sum(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    if not inputs.line_items:
        return []
    line_sum = quantize_amount(
        sum((line.amount for line in inputs.line_items), Decimal(0)), inputs.currency
    )
    tolerance = amount_tolerance(inputs.currency) * max(1, len(inputs.line_items))
    if abs(line_sum - inputs.subtotal) > tolerance:
        return [
            Finding(
                rule_id="V-002",
                severity="hard_fail",
                field="subtotal",
                message="Sum of line amounts does not equal subtotal",
                expected=str(line_sum),
                actual=str(inputs.subtotal),
            )
        ]
    return []


def rule_v003_total_math(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    expected = quantize_amount(inputs.subtotal + inputs.tax_amount, inputs.currency)
    if abs(expected - inputs.total) > amount_tolerance(inputs.currency):
        return [
            Finding(
                rule_id="V-003",
                severity="hard_fail",
                field="total",
                message="subtotal + tax_amount does not equal total",
                expected=str(expected),
                actual=str(inputs.total),
            )
        ]
    return []


def rule_v004_tax_rate(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    if inputs.subtotal <= 0 or inputs.tax_amount <= 0:
        return []
    rate = inputs.tax_amount / inputs.subtotal
    if rate > Decimal("0.50"):
        return [
            Finding(
                rule_id="V-004",
                severity="flag",
                field="tax_amount",
                message="Implied tax rate exceeds 50%",
                expected="<= 0.50",
                actual=f"{rate:.4f}",
            )
        ]
    return []


def rule_v005_currency(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    if inputs.currency not in ctx.supported_currencies:
        return [
            Finding(
                rule_id="V-005",
                severity="hard_fail",
                field="currency",
                message="Currency is not in the supported ISO-4217 allowlist",
                expected=", ".join(sorted(ctx.supported_currencies)[:8]) + ", …",
                actual=inputs.currency,
            )
        ]
    return []


def rule_v006_non_negative(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    findings = []
    for line in inputs.line_items:
        if line.quantity <= 0:
            findings.append(
                Finding(
                    rule_id="V-006",
                    severity="hard_fail",
                    field=f"line_items[{line.line_no}].quantity",
                    message="Quantity must be positive",
                    actual=str(line.quantity),
                )
            )
        if line.unit_price < 0 or line.amount < 0:
            findings.append(
                Finding(
                    rule_id="V-006",
                    severity="hard_fail",
                    field=f"line_items[{line.line_no}]",
                    message="Line price/amount must be non-negative",
                    actual=f"unit_price={line.unit_price}, amount={line.amount}",
                )
            )
    for name, value in (
        ("subtotal", inputs.subtotal),
        ("tax_amount", inputs.tax_amount),
        ("total", inputs.total),
    ):
        if value < 0:
            findings.append(
                Finding(
                    rule_id="V-006",
                    severity="hard_fail",
                    field=name,
                    message=f"{name} must be non-negative",
                    actual=str(value),
                )
            )
    return findings


def rule_v007_lines_present(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    if not inputs.line_items:
        return [
            Finding(
                rule_id="V-007",
                severity="hard_fail",
                field="line_items",
                message="Invoice has no line items",
            )
        ]
    return []


def rule_v008_invoice_date_sane(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    today = ctx.today
    upper = today + timedelta(days=1)
    lower = today - timedelta(days=3653)
    if inputs.invoice_date > upper:
        return [
            Finding(
                rule_id="V-008",
                severity="hard_fail",
                field="invoice_date",
                message="Invoice date is in the future",
                expected=f"<= {upper}",
                actual=str(inputs.invoice_date),
            )
        ]
    if inputs.invoice_date < lower:
        return [
            Finding(
                rule_id="V-008",
                severity="hard_fail",
                field="invoice_date",
                message="Invoice date is implausibly old (>10 years)",
                expected=f">= {lower}",
                actual=str(inputs.invoice_date),
            )
        ]
    return []


def rule_v009_due_after_invoice(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    if inputs.due_date is not None and inputs.due_date < inputs.invoice_date:
        return [
            Finding(
                rule_id="V-009",
                severity="hard_fail",
                field="due_date",
                message="Due date precedes invoice date",
                expected=f">= {inputs.invoice_date}",
                actual=str(inputs.due_date),
            )
        ]
    return []


def rule_v010_invoice_before_po(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    if ctx.po_order_date is not None and inputs.invoice_date < ctx.po_order_date:
        return [
            Finding(
                rule_id="V-010",
                severity="flag",
                field="invoice_date",
                message="Invoice is dated before its purchase order",
                expected=f">= {ctx.po_order_date}",
                actual=str(inputs.invoice_date),
            )
        ]
    return []


def rule_v011_duplicate_lines(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    seen: dict[tuple[str, Decimal, Decimal], int] = {}
    findings = []
    for line in inputs.line_items:
        key = (line.description.strip().casefold(), line.quantity, line.unit_price)
        if key in seen:
            findings.append(
                Finding(
                    rule_id="V-011",
                    severity="flag",
                    field=f"line_items[{line.line_no}]",
                    message=(
                        f"Line duplicates line {seen[key]} (same description, quantity, unit price)"
                    ),
                )
            )
        else:
            seen[key] = line.line_no
    return findings


def rule_v012_vendor_known(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    if not ctx.vendor_known:
        return [
            Finding(
                rule_id="V-012",
                severity="flag",
                field="vendor_name",
                message="Vendor is not in the vendor master (new or unrecognized)",
            )
        ]
    return []


def rule_v013_low_confidence(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    low = sorted(
        (name, conf)
        for name, conf in inputs.field_confidence.items()
        if conf < ctx.confidence_floor
    )
    if low:
        listing = ", ".join(f"{name}={conf:.2f}" for name, conf in low)
        return [
            Finding(
                rule_id="V-013",
                severity="flag",
                field=low[0][0],
                message=f"Low extraction confidence below {ctx.confidence_floor}: {listing}",
            )
        ]
    return []


def rule_v014_zero_total(inputs: ValidationInput, ctx: RuleContext) -> list[Finding]:
    if inputs.total == 0:
        return [
            Finding(
                rule_id="V-014",
                severity="flag",
                field="total",
                message="Invoice total is zero",
            )
        ]
    return []


RULES: tuple[Rule, ...] = (
    rule_v001_line_math,
    rule_v002_line_sum,
    rule_v003_total_math,
    rule_v004_tax_rate,
    rule_v005_currency,
    rule_v006_non_negative,
    rule_v007_lines_present,
    rule_v008_invoice_date_sane,
    rule_v009_due_after_invoice,
    rule_v010_invoice_before_po,
    rule_v011_duplicate_lines,
    rule_v012_vendor_known,
    rule_v013_low_confidence,
    rule_v014_zero_total,
)


def run_rules(inputs: ValidationInput, ctx: RuleContext) -> ValidationResult:
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(inputs, ctx))
    return ValidationResult(findings=findings)


# --- pipeline stage ----------------------------------------------------------------


def inputs_from_invoice(invoice: Invoice) -> ValidationInput:
    if invoice.invoice_date is None:
        raise ValueError("validation requires an extracted invoice_date")
    return ValidationInput(
        currency=invoice.currency or "",
        line_items=[
            LineInput(
                line_no=line.line_no,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
            )
            for line in invoice.line_items
        ],
        subtotal=invoice.subtotal or Decimal(0),
        tax_amount=invoice.tax_amount or Decimal(0),
        total=invoice.total or Decimal(0),
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        field_confidence=dict(invoice.field_confidence or {}),
    )


async def run_validation_stage(
    session: AsyncSession, invoice: Invoice, *, settings: Settings
) -> bool:
    """Validate an EXTRACTED invoice. Returns True when it may proceed; on hard
    failure routes to EXCEPTION and returns False. Commits either way."""
    # Line items were inserted by the extraction stage after this ORM object was
    # loaded; refresh the relationship explicitly (async lazy-loads are forbidden).
    await session.refresh(invoice, attribute_names=["line_items"])
    ctx = RuleContext(
        today=datetime.now(UTC).date(),
        supported_currencies=frozenset(settings.supported_currencies),
        confidence_floor=settings.confidence_review_floor,
        vendor_known=invoice.vendor_id is not None,
    )
    result = run_rules(inputs_from_invoice(invoice), ctx)
    invoice.validation_results = result.to_jsonb()

    failed_rules = sorted({f.rule_id for f in result.findings if f.severity == "hard_fail"})
    flags = sorted({f.rule_id for f in result.findings if f.severity == "flag"})
    audit.record(
        session,
        entity_type="invoice",
        entity_id=invoice.id,
        invoice_id=invoice.id,
        event_type="validation_completed",
        actor="worker",
        payload={"passed": result.passed, "hard_fails": failed_rules, "flags": flags},
    )

    if not result.passed:
        await transition(
            session,
            invoice,
            InvoiceStatus.EXCEPTION,
            actor="worker",
            reason="validation",
            payload={"failed_rules": failed_rules},
        )
        await session.commit()
        return False

    await session.commit()
    return True
