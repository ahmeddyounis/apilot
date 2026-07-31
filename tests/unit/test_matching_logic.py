"""Deterministic matcher logic: alignment, diffs, proposed-pair re-validation, gate."""

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import Matcher
from app.agent.state import InvoiceLineData, MatchingState, PoData, PoLineData
from app.core.config import Settings
from app.llm.fake import FakeLLM
from app.schemas.matching import InvestigationOutcome

pytestmark = pytest.mark.unit


def make_matcher(**settings_overrides: Any) -> Matcher:
    settings = Settings(_env_file=None, **settings_overrides)
    return Matcher(session=cast(AsyncSession, None), llm=FakeLLM(), settings=settings)


def inv_line(index: int, desc: str, qty: str, price: str) -> InvoiceLineData:
    amount = Decimal(qty) * Decimal(price)
    return InvoiceLineData(
        index=index,
        description=desc,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        amount=amount.quantize(Decimal("0.01")),
    )


def po_line(index: int, line_no: int, desc: str, qty: str, price: str) -> PoLineData:
    return PoLineData(
        index=index,
        line_no=line_no,
        sku=None,
        description=desc,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
    )


def make_state(
    invoice_lines: list[InvoiceLineData],
    po_lines: list[PoLineData],
    *,
    received: dict[int, str] | None = None,
    total: str = "507.60",
    fraud: list[str] | None = None,
) -> MatchingState:
    vendor_id = uuid4()
    po = PoData(
        id=uuid4(),
        po_number="PO-2231",
        vendor_id=vendor_id,
        currency="USD",
        total=Decimal("470.00"),
        order_date="2026-06-20",
        status="open",
        lines=po_lines,
    )
    return MatchingState(
        invoice_id=uuid4(),
        trace_id="t-1",
        currency="USD",
        total=Decimal(total),
        vendor_id=vendor_id,
        vendor_name="ACME Office Supply Co",
        invoice_number="INV-1",
        po_number_raw="PO-2231",
        invoice_date="2026-07-05",
        fraud_flag_codes=fraud or [],
        invoice_lines=invoice_lines,
        po=po,
        po_resolution="by_number",
        received_by_po_line={int(k): Decimal(v) for k, v in (received or {}).items()},
    )


async def test_line_align_exact_fuzzy_and_reject() -> None:
    matcher = make_matcher()
    state = make_state(
        [
            inv_line(0, "Widget A-200", "10", "47.00"),
            inv_line(1, "Ballpoint pens (blue), boxed", "5", "3.00"),
            inv_line(2, "Consulting retainer July", "1", "500.00"),
        ],
        [
            po_line(0, 1, "Widget A-200", "10", "47.00"),
            po_line(1, 2, "Blue ballpoint pens box", "5", "3.00"),
            po_line(2, 3, "Steel bolts M8", "100", "0.10"),
        ],
    )
    updates = await matcher.line_align(state)
    pairs = {(p.invoice_index, p.po_index): p for p in updates["alignment"]}
    assert (0, 0) in pairs and pairs[(0, 0)].similarity == 1.0
    assert (1, 1) in pairs and pairs[(1, 1)].similarity >= 0.6
    assert updates["unmatched_invoice"] == [2]
    assert updates["unmatched_po"] == [2]


async def test_compute_diffs_overbill_matches_spec_impact() -> None:
    matcher = make_matcher()
    state = make_state(
        [inv_line(0, "Widget A-200", "12", "47.00")],
        [po_line(0, 1, "Widget A-200", "10", "47.00")],
        received={1: "10"},
    )
    align = await matcher.line_align(state)
    state = state.model_copy(update={k: v for k, v in align.items() if k != "event_summary"})
    updates = await matcher.compute_diffs(state)
    discrepancies = updates["discrepancies"]
    assert [d.type for d in discrepancies] == ["quantity_mismatch"]
    assert discrepancies[0].impact is not None
    assert discrepancies[0].impact.amount == "94.00"  # (12-10) x 47.00 — the spec example
    checks = updates["checks"]
    assert checks.quantity_match is False
    assert checks.receipt_match is True  # delivery complete per the PO
    assert checks.price_match is True


async def test_compute_diffs_price_drift_and_shortfall() -> None:
    matcher = make_matcher()
    state = make_state(
        [inv_line(0, "Widget A-200", "10", "51.00")],
        [po_line(0, 1, "Widget A-200", "10", "47.00")],
        received={1: "6"},
    )
    align = await matcher.line_align(state)
    state = state.model_copy(update={k: v for k, v in align.items() if k != "event_summary"})
    updates = await matcher.compute_diffs(state)
    by_type = {d.type: d for d in updates["discrepancies"]}
    assert by_type["price_mismatch"].impact is not None
    assert by_type["price_mismatch"].impact.amount == "40.00"  # (51-47) x 10
    assert by_type["receipt_shortfall"].received_qty == "6"
    assert updates["needs_investigation"] is True


async def test_assemble_verdict_gate_boundary_and_fraud() -> None:
    matcher = make_matcher()

    async def verdict_for(total: str, fraud: list[str] | None = None) -> Any:
        state = make_state(
            [inv_line(0, "Widget A-200", "10", "47.00")],
            [po_line(0, 1, "Widget A-200", "10", "47.00")],
            received={1: "10"},
            total=total,
            fraud=fraud,
        )
        align = await matcher.line_align(state)
        state = state.model_copy(update={k: v for k, v in align.items() if k != "event_summary"})
        diffs = await matcher.compute_diffs(state)
        state = state.model_copy(update={k: v for k, v in diffs.items() if k != "event_summary"})
        result = await matcher.assemble_verdict(state)
        return result["verdict"]

    at_threshold = await verdict_for("2500.00")
    assert at_threshold.verdict == "auto_approve"

    over_threshold = await verdict_for("2500.01")
    assert over_threshold.verdict == "exception"
    assert over_threshold.checks.all_pass()  # clean match — blocked only by amount
    assert "threshold" in over_threshold.agent_summary

    flagged = await verdict_for("100.00", fraud=["F-110"])
    assert flagged.verdict == "exception"
    assert flagged.fraud_flags == ["F-110"]


async def test_missing_po_verdict() -> None:
    matcher = make_matcher()
    state = MatchingState(
        invoice_id=uuid4(),
        trace_id="t-2",
        currency="USD",
        total=Decimal("100.00"),
        po_number_raw="PO-NOPE",
        po_resolution="not_found",
        invoice_lines=[inv_line(0, "Anything", "1", "100.00")],
    )
    result = await matcher.assemble_verdict(state)
    verdict = result["verdict"]
    assert verdict.verdict == "exception"
    assert verdict.checks.po_found is False
    assert verdict.discrepancies[0].type == "missing_po"


def test_apply_proposed_pairs_validates() -> None:
    matcher = make_matcher()
    state = make_state(
        [inv_line(0, "A4 copy paper 80gsm", "10", "5.00"), inv_line(1, "Unrelated", "1", "1.00")],
        [
            po_line(0, 1, "Paper, copy, A4 (80gsm)", "10", "5.00"),
            po_line(1, 2, "Steel bolts", "9", "1.00"),
        ],
    )
    state = state.model_copy(
        update={"alignment": [], "unmatched_invoice": [0, 1], "unmatched_po": [0, 1]}
    )
    outcome = InvestigationOutcome(
        proposed_pairs=[
            [0, 0],  # plausible rewording → accepted
            [1, 1],  # dissimilar → rejected by similarity floor
            [7, 9],  # bogus indexes → ignored
            [0],  # malformed → ignored
        ],
        summary="s",
    )
    updates = matcher._apply_proposed_pairs(state, outcome)
    assert updates["_accepted_pairs"] == 1
    assert [p.source for p in updates["alignment"]] == ["llm_proposed"]
    assert updates["unmatched_invoice"] == [1]
    assert updates["unmatched_po"] == [1]


def test_parse_outcome_json_tail_and_garbage() -> None:
    matcher = make_matcher()
    good = matcher._parse_outcome(
        'Some prose first. {"proposed_pairs": [[1, 2]], "summary": "done"}'
    )
    assert good.proposed_pairs == [[1, 2]]
    assert good.summary == "done"

    fallback = matcher._parse_outcome("I could not decide anything definitive.")
    assert fallback.proposed_pairs == []
    assert "definitive" in fallback.summary
