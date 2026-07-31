"""Human review: approve / reject / correct.

Corrections update the typed columns (never the immutable ``extraction`` JSONB) and
are stored in ``field_corrections`` — the ground truth that feeds the eval dataset.
Deterministic validation re-runs synchronously after a correction; the invoice stays
in the queue until a human approves (or asks for a matching re-run).
"""

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext
from app.api.errors import ApilotError, InvalidStateError
from app.core.config import Settings
from app.core.normalize import normalize_invoice_number
from app.models import FieldCorrection, Invoice, InvoiceStatus, ReviewAction
from app.services import audit
from app.services.invoices import enqueue_pipeline
from app.services.state_machine import transition
from app.services.validation import RuleContext, inputs_from_invoice, run_rules

_REVIEWABLE = (InvoiceStatus.EXCEPTION, InvoiceStatus.HELD)

_LINE_PATH = re.compile(r"^line_items\[(\d+)\]\.(description|quantity|unit_price|amount)$")

_HEADER_PARSERS: dict[str, Any] = {
    "vendor_name": str,
    "invoice_number": str,
    "invoice_date": date.fromisoformat,
    "due_date": lambda v: date.fromisoformat(v) if v is not None else None,
    "currency": lambda v: str(v).upper(),
    "po_number": lambda v: str(v) if v is not None else None,
    "subtotal": Decimal,
    "tax_amount": Decimal,
    "total": Decimal,
}


class CorrectionError(ApilotError):
    status_code = 422
    code = "invalid_correction"


def _apply_correction(invoice: Invoice, field_path: str, new_value: Any) -> Any:
    """Apply one correction; returns the old value. Raises CorrectionError."""
    line_match = _LINE_PATH.match(field_path)
    if line_match:
        line_no, attr = int(line_match.group(1)), line_match.group(2)
        line = next((li for li in invoice.line_items if li.line_no == line_no), None)
        if line is None:
            raise CorrectionError(f"No line item with line_no {line_no}")
        old = getattr(line, attr)
        try:
            value = str(new_value) if attr == "description" else Decimal(str(new_value))
        except InvalidOperation as exc:
            raise CorrectionError(f"'{new_value}' is not a valid amount") from exc
        setattr(line, attr, value)
        return old

    parser = _HEADER_PARSERS.get(field_path)
    if parser is None:
        valid = ", ".join(sorted(_HEADER_PARSERS))
        raise CorrectionError(
            f"Unknown field_path '{field_path}'. Correctable: {valid}, "
            f"line_items[N].<description|quantity|unit_price|amount>"
        )
    old = getattr(invoice, field_path)
    try:
        parsed = parser(new_value)
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise CorrectionError(f"Invalid value for {field_path}: {new_value!r}") from exc
    setattr(invoice, field_path, parsed)
    if field_path == "invoice_number":
        invoice.normalized_invoice_number = normalize_invoice_number(parsed)
    return old


async def apply_review(
    session: AsyncSession,
    invoice: Invoice,
    *,
    auth: AuthContext,
    action: str,
    note: str | None,
    corrections: list[tuple[str, Any]] | None,
    rerun_matching: bool,
    settings: Settings,
) -> tuple[int, bool | None]:
    """Returns (corrections_applied, validation_passed|None). Commits."""
    review = ReviewAction(invoice_id=invoice.id, api_key_id=auth.key_id, action=action, note=note)
    session.add(review)
    await session.flush()

    if action == "approve":
        await transition(
            session, invoice, InvoiceStatus.APPROVED, actor=auth.actor, payload={"note": note}
        )
        await session.commit()
        return 0, None

    if action == "reject":
        await transition(
            session, invoice, InvoiceStatus.REJECTED, actor=auth.actor, payload={"note": note}
        )
        await session.commit()
        return 0, None

    # action == "correct"
    if invoice.status not in _REVIEWABLE:
        raise InvalidStateError(f"Cannot correct invoice in status '{invoice.status.value}'")
    if not corrections:
        raise CorrectionError("action 'correct' requires at least one correction")

    await session.refresh(invoice, attribute_names=["line_items"])
    applied = 0
    for field_path, new_value in corrections:
        old_value = _apply_correction(invoice, field_path, new_value)
        session.add(
            FieldCorrection(
                invoice_id=invoice.id,
                review_action_id=review.id,
                field_path=field_path,
                old_value=_jsonable(old_value),
                new_value=_jsonable(new_value),
            )
        )
        applied += 1

    # Re-run deterministic validation on the corrected values, synchronously.
    ctx = RuleContext(
        today=datetime.now(UTC).date(),
        supported_currencies=frozenset(settings.supported_currencies),
        confidence_floor=settings.confidence_review_floor,
        vendor_known=invoice.vendor_id is not None,
    )
    result = run_rules(inputs_from_invoice(invoice), ctx)
    invoice.validation_results = result.to_jsonb()

    audit.record(
        session,
        entity_type="invoice",
        entity_id=invoice.id,
        invoice_id=invoice.id,
        event_type="review_correction",
        actor=auth.actor,
        payload={
            "fields": [path for path, _ in corrections],
            "validation_passed": result.passed,
            "note": note,
        },
    )

    if rerun_matching:
        # A human release is an explicit override: the screening flags move into
        # the audit trail (they stay queryable there) and stop blocking the rerun —
        # otherwise a held duplicate could never be released for re-matching.
        cleared_flags = [flag.get("code", "?") for flag in invoice.fraud_flags or []]
        invoice.fraud_flags = []
        invoice.risk_score = None
        invoice.hold_reason = None
        await transition(
            session,
            invoice,
            InvoiceStatus.MATCHING,
            actor=auth.actor,
            payload={"trigger": "review_correction", "cleared_flags": cleared_flags},
        )
        await session.commit()
        await enqueue_pipeline(invoice.id)
    else:
        await session.commit()
    return applied, result.passed


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal | date):
        return str(value)
    return value
