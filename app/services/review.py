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
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext
from app.api.errors import ApilotError, InvalidStateError
from app.core.config import Settings
from app.core.normalize import normalize_invoice_number
from app.models import FieldCorrection, Invoice, InvoiceStatus, ReviewAction
from app.services import audit
from app.services.dedup import trust_payment_fingerprint
from app.services.invoices import enqueue_pipeline
from app.services.state_machine import transition
from app.services.validation import RuleContext, inputs_from_invoice, run_rules

_REVIEWABLE = (InvoiceStatus.EXCEPTION, InvoiceStatus.HELD)

_LINE_PATH = re.compile(r"^line_items\[(\d+)\]\.(description|quantity|unit_price|amount)$")


def _text(value: Any, *, max_length: int, optional: bool = False) -> str | None:
    """Corrections are ground truth for the eval dataset, so they have to be text a
    human actually typed — bare str() happily turned None into "None" and a JSON
    object into "{'evil': 1}", then promote_corrections.py labelled it as truth."""
    if value is None:
        if optional:
            return None
        raise ValueError("value is required")
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError("value cannot be blank")
    if len(text) > max_length:
        raise ValueError(f"value is longer than {max_length} characters")
    return text


def _money(value: Any) -> Decimal:
    """Decimal() accepts "NaN" and "1e40". Both parse, so the guard below never
    fired; the failure surfaced later as an InvalidOperation inside the rules
    engine or a numeric overflow at commit — a 500 either way, from one JSON body.
    The bound matches NUMERIC(14,2), the column these land in."""
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError(f"'{value}' is not a finite amount")
    if abs(amount) >= Decimal(10) ** 12:
        raise ValueError(f"'{value}' is too large to store")
    return amount


def _currency(value: Any) -> str:
    code = _text(value, max_length=3)
    assert code is not None
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"'{value}' is not a three-letter ISO 4217 code")
    return code.upper()


_HEADER_PARSERS: dict[str, Any] = {
    "vendor_name": lambda v: _text(v, max_length=255),
    "invoice_number": lambda v: _text(v, max_length=100),
    "invoice_date": date.fromisoformat,
    "due_date": lambda v: date.fromisoformat(v) if v is not None else None,
    "currency": _currency,
    "po_number": lambda v: _text(v, max_length=100, optional=True),
    "subtotal": _money,
    "tax_amount": _money,
    "total": _money,
}


class CorrectionError(ApilotError):
    status_code = 422
    code = "invalid_correction"


async def load_for_review(session: AsyncSession, invoice_id: UUID) -> Invoice | None:
    """Fetch an invoice with a row lock held for the rest of the transaction.

    Review decisions are financial: without the lock, two reviewers acting at the
    same time both read the pre-decision status, both find their transition legal,
    and both commit — the invoice lands on whichever wrote last while the audit log
    records both as having succeeded. FOR UPDATE serializes them, so the second
    reader sees the decided status and gets a 409.
    """
    return await session.scalar(select(Invoice).where(Invoice.id == invoice_id).with_for_update())


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
            value = _text(new_value, max_length=500) if attr == "description" else _money(new_value)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise CorrectionError(f"Invalid value for {field_path}: {new_value!r}") from exc
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
        # An approval is the human evidence screening can't produce on its own: if
        # this invoice carried changed bank details, the reviewer has now vouched
        # for them, so the account becomes known-good for future invoices.
        trusted = await trust_payment_fingerprint(session, invoice)
        await transition(
            session,
            invoice,
            InvoiceStatus.APPROVED,
            actor=auth.actor,
            payload={"note": note, "trusted_payment_fingerprint": trusted},
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
