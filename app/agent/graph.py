"""The LangGraph 3-way matching workflow.

Deterministic-first: PO lookup, line alignment (exact + Hungarian assignment), and
discrepancy arithmetic are plain code. The single LLM node investigates ambiguity
(bounded ≤ 4 tool turns, read-only tools, direct Anthropic SDK via our client — no
langchain model wrapper) and may *propose* line pairings; code re-validates every
proposal and recomputes the diffs. The verdict is assembled by code — the LLM cannot
approve anything.

Graph:
    po_lookup → [no PO → assemble_verdict]
              → line_align → compute_diffs → [ambiguity? → investigate → compute_diffs]
              → assemble_verdict
"""

import json
import time
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import structlog
from langgraph.graph import END, START, StateGraph
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tools
from app.agent.state import AlignedPair, MatchingState, NodeEvent
from app.core.config import Settings
from app.core.money import quantize_amount
from app.llm.client import LLMClient
from app.llm.types import FixtureMissingError
from app.schemas.matching import (
    Discrepancy,
    Impact,
    InvestigationOutcome,
    MatchingChecks,
    MatchingVerdict,
)
from app.services.cost_ledger import record_llm_call

logger = structlog.get_logger(__name__)

MATCHING_SYSTEM = (
    Path(__file__).resolve().parent.parent / "llm" / "prompts" / "matching_system.md"
).read_text()

_MIN_PAIR_SIMILARITY = 0.60
_MIN_PROPOSED_SIMILARITY = 0.40
_MAX_TOOL_TURNS = 4

NodeUpdates = dict[str, Any]


class Matcher:
    def __init__(self, *, session: AsyncSession, llm: LLMClient, settings: Settings) -> None:
        self.session = session
        self.llm = llm
        self.settings = settings
        self.graph = self._build()

    # --- graph assembly ----------------------------------------------------------

    def _build(self) -> Any:
        graph = StateGraph(MatchingState)
        # cast: langgraph's add_node overloads don't accept plain async callables
        # returning dict updates, though that is the documented usage.
        nodes: tuple[tuple[str, Callable[[MatchingState], Awaitable[NodeUpdates]]], ...] = (
            ("po_lookup", self.po_lookup),
            ("line_align", self.line_align),
            ("compute_diffs", self.compute_diffs),
            ("investigate", self.investigate),
            ("assemble_verdict", self.assemble_verdict),
        )
        for name, fn in nodes:
            graph.add_node(name, cast(Any, self._timed(name, fn)))

        graph.add_edge(START, "po_lookup")
        graph.add_conditional_edges(
            "po_lookup",
            lambda state: "line_align" if state.po is not None else "assemble_verdict",
        )
        graph.add_edge("line_align", "compute_diffs")
        graph.add_conditional_edges(
            "compute_diffs",
            lambda state: (
                "investigate"
                if state.needs_investigation and not state.investigated
                else "assemble_verdict"
            ),
        )
        graph.add_edge("investigate", "compute_diffs")
        graph.add_edge("assemble_verdict", END)
        return graph.compile()

    def _timed(
        self, name: str, fn: Callable[[MatchingState], Awaitable[NodeUpdates]]
    ) -> Callable[[MatchingState], Awaitable[NodeUpdates]]:
        async def wrapped(state: MatchingState) -> NodeUpdates:
            started = time.monotonic()
            updates = await fn(state)
            duration_ms = int((time.monotonic() - started) * 1000)
            summary = updates.pop("event_summary", {})
            event = NodeEvent(
                seq=len(state.events) + 1, node=name, duration_ms=duration_ms, summary=summary
            )
            updates["events"] = [*state.events, event]
            return updates

        return wrapped

    async def run(self, state: MatchingState) -> MatchingState:
        result = await self.graph.ainvoke(state)
        return MatchingState.model_validate(result)

    # --- deterministic nodes -----------------------------------------------------

    async def po_lookup(self, state: MatchingState) -> NodeUpdates:
        raw = (state.po_number_raw or "").strip()
        if raw:
            po = await tools.lookup_po(self.session, raw)
            if po is not None:
                received = await tools.lookup_receipts(self.session, po.id)
                billed = await tools.po_billed_total(
                    self.session, po_id=po.id, exclude_invoice_id=state.invoice_id
                )
                return {
                    "po": po,
                    "po_resolution": "by_number",
                    "received_by_po_line": received,
                    "po_billed_elsewhere": billed,
                    "event_summary": {
                        "po_number": po.po_number,
                        "resolution": "by_number",
                        "already_billed": str(billed),
                    },
                }
            resolution = "not_found"
        else:
            resolution = "no_po_number"

        # Bounded fallback search; exactly one candidate is usable, anything else is
        # a human problem — guessing purchase orders is the one thing an AP system
        # must never do.
        if state.vendor_id is not None and state.invoice_date is not None:
            candidates = await tools.find_po_candidates(
                self.session,
                vendor_id=state.vendor_id,
                total=state.total,
                invoice_date=date.fromisoformat(state.invoice_date),
            )
            if len(candidates) == 1:
                po = candidates[0]
                received = await tools.lookup_receipts(self.session, po.id)
                billed = await tools.po_billed_total(
                    self.session, po_id=po.id, exclude_invoice_id=state.invoice_id
                )
                return {
                    "po": po,
                    "po_resolution": "fuzzy",
                    "received_by_po_line": received,
                    "po_billed_elsewhere": billed,
                    "event_summary": {
                        "po_number": po.po_number,
                        "resolution": "fuzzy",
                        "note": "single candidate by vendor/total/date window",
                    },
                }
            return {
                "po_resolution": resolution,
                "event_summary": {"resolution": resolution, "candidates": len(candidates)},
            }
        return {"po_resolution": resolution, "event_summary": {"resolution": resolution}}

    async def line_align(self, state: MatchingState) -> NodeUpdates:
        assert state.po is not None
        invoice_lines = state.invoice_lines
        po_lines = state.po.lines

        pairs: list[AlignedPair] = []
        used_invoice: set[int] = set()
        used_po: set[int] = set()

        # Pass 1: exact normalized-description matches.
        po_by_desc: dict[str, list[int]] = {}
        for line in po_lines:
            po_by_desc.setdefault(line.description.strip().casefold(), []).append(line.index)
        for inv in invoice_lines:
            key = inv.description.strip().casefold()
            for po_index in po_by_desc.get(key, []):
                if po_index not in used_po:
                    pairs.append(
                        AlignedPair(invoice_index=inv.index, po_index=po_index, similarity=1.0)
                    )
                    used_invoice.add(inv.index)
                    used_po.add(po_index)
                    break

        # Pass 2: optimal assignment (Hungarian) over the remainder on fuzzy
        # similarity — optimal and order-independent, unlike greedy matching.
        rest_inv = [line for line in invoice_lines if line.index not in used_invoice]
        rest_po = [line for line in po_lines if line.index not in used_po]
        if rest_inv and rest_po:
            cost = [
                [
                    1.0 - fuzz.token_set_ratio(inv.description, po.description) / 100.0
                    for po in rest_po
                ]
                for inv in rest_inv
            ]
            row_indexes, col_indexes = linear_sum_assignment(cost)
            for row, col in zip(row_indexes, col_indexes, strict=True):
                similarity = 1.0 - cost[row][col]
                if similarity >= _MIN_PAIR_SIMILARITY:
                    pairs.append(
                        AlignedPair(
                            invoice_index=rest_inv[row].index,
                            po_index=rest_po[col].index,
                            similarity=round(similarity, 3),
                        )
                    )
                    used_invoice.add(rest_inv[row].index)
                    used_po.add(rest_po[col].index)

        unmatched_invoice = [line.index for line in invoice_lines if line.index not in used_invoice]
        unmatched_po = [line.index for line in po_lines if line.index not in used_po]
        return {
            "alignment": pairs,
            "unmatched_invoice": unmatched_invoice,
            "unmatched_po": unmatched_po,
            "event_summary": {
                "aligned": len(pairs),
                "unmatched_invoice": len(unmatched_invoice),
                "unmatched_po": len(unmatched_po),
            },
        }

    async def compute_diffs(self, state: MatchingState) -> NodeUpdates:
        assert state.po is not None
        po = state.po
        currency = state.currency
        inv_by_index = {line.index: line for line in state.invoice_lines}
        po_by_index = {line.index: line for line in po.lines}
        price_tol = self.settings.price_tolerance_pct
        qty_tol = self.settings.qty_tolerance_pct

        discrepancies: list[Discrepancy] = []
        qty_ok = True
        price_ok = True

        for pair in state.alignment:
            inv = inv_by_index[pair.invoice_index]
            pol = po_by_index[pair.po_index]

            allowed_qty = pol.quantity * (1 + qty_tol)
            if inv.quantity > allowed_qty:
                qty_ok = False
                over = inv.quantity - pol.quantity
                discrepancies.append(
                    Discrepancy(
                        type="quantity_mismatch",
                        line=inv.description,
                        invoice_qty=str(inv.quantity),
                        po_qty=str(pol.quantity),
                        impact=Impact(
                            amount=str(quantize_amount(over * inv.unit_price, currency)),
                            currency=currency,
                        ),
                    )
                )

            if pol.unit_price > 0:
                drift = abs(inv.unit_price - pol.unit_price) / pol.unit_price
                if drift > price_tol:
                    price_ok = False
                    delta = (inv.unit_price - pol.unit_price) * inv.quantity
                    discrepancies.append(
                        Discrepancy(
                            type="price_mismatch",
                            line=inv.description,
                            invoice_price=str(inv.unit_price),
                            po_price=str(pol.unit_price),
                            impact=Impact(
                                amount=str(quantize_amount(delta, currency)),
                                currency=currency,
                            ),
                        )
                    )
            elif inv.unit_price > 0:
                # A PO line priced at zero (freight placeholder, "TBD", sample) can't
                # produce a drift ratio, but skipping the check outright meant the
                # vendor could bill any amount at all on that line and still match.
                price_ok = False
                discrepancies.append(
                    Discrepancy(
                        type="price_mismatch",
                        line=inv.description,
                        invoice_price=str(inv.unit_price),
                        po_price=str(pol.unit_price),
                        impact=Impact(
                            amount=str(quantize_amount(inv.unit_price * inv.quantity, currency)),
                            currency=currency,
                        ),
                        note="purchase-order line carries no price to check against",
                    )
                )

            received = state.received_by_po_line.get(pol.line_no, Decimal(0))
            billable = min(inv.quantity, pol.quantity)
            if billable > received:
                discrepancies.append(
                    Discrepancy(
                        type="receipt_shortfall",
                        line=inv.description,
                        invoice_qty=str(inv.quantity),
                        received_qty=str(received),
                        impact=Impact(
                            amount=str(
                                quantize_amount((billable - received) * inv.unit_price, currency)
                            ),
                            currency=currency,
                        ),
                        note="billed ahead of recorded delivery",
                    )
                )

        for index in state.unmatched_invoice:
            inv = inv_by_index[index]
            discrepancies.append(
                Discrepancy(
                    type="unmatched_line",
                    line=inv.description,
                    invoice_qty=str(inv.quantity),
                    impact=Impact(amount=str(inv.amount), currency=currency),
                    note="no corresponding purchase-order line",
                )
            )

        if currency != po.currency:
            discrepancies.append(
                Discrepancy(
                    type="currency_mismatch",
                    note=f"invoice {currency} vs purchase order {po.currency}",
                )
            )

        # Delivery must cover what is *billed*, not the whole ordered quantity:
        # comparing against the PO line made legitimate partial billing (5 of 10
        # ordered, 5 delivered) fail with no discrepancy to explain why, while
        # over-billing is caught by receipt_shortfall above, which uses the same
        # billable figure. `all([])` is vacuously true, so an invoice where nothing
        # aligned used to publish receipt_match: true having consulted no receipt.
        receipt_ok = bool(state.alignment) and all(
            state.received_by_po_line.get(po_by_index[pair.po_index].line_no, Decimal(0))
            >= min(
                inv_by_index[pair.invoice_index].quantity,
                po_by_index[pair.po_index].quantity,
            )
            for pair in state.alignment
        )

        # Cumulative consumption. Every invoice was matched against the PO in
        # isolation, so the same authorisation could be billed to exhaustion: two
        # invoices for the full delivery both auto-approved, and splitting the
        # quantity across invoices evaded the near-duplicate screen as well (the
        # totals differ, so nothing looked like a resubmission).
        already_billed = state.po_billed_elsewhere
        if already_billed > 0 and already_billed + state.total > po.total:
            discrepancies.append(
                Discrepancy(
                    type="po_over_consumption",
                    impact=Impact(
                        amount=str(
                            quantize_amount(already_billed + state.total - po.total, currency)
                        ),
                        currency=currency,
                    ),
                    note=(
                        f"purchase order authorises {po.total} {po.currency}; "
                        f"{already_billed} already billed against it by other invoices"
                    ),
                )
            )

        vendor_ok = state.vendor_id is not None and state.vendor_id == po.vendor_id
        checks = MatchingChecks(
            po_found=True,
            vendor_match=vendor_ok,
            quantity_match=qty_ok and not state.unmatched_invoice,
            price_match=price_ok,
            receipt_match=receipt_ok,
        )
        needs_investigation = any(d.type == "price_mismatch" for d in discrepancies) or bool(
            state.unmatched_invoice and state.unmatched_po
        )
        return {
            "discrepancies": discrepancies,
            "checks": checks,
            "needs_investigation": needs_investigation,
            "event_summary": {
                "discrepancies": [d.type for d in discrepancies],
                "checks": checks.model_dump(),
            },
        }

    # --- the LLM node ------------------------------------------------------------

    async def investigate(self, state: MatchingState) -> NodeUpdates:
        assert state.po is not None
        brief = self._investigation_brief(state)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": brief}]}
        ]
        turns: list[dict[str, Any]] = []

        for turn_number in range(1, _MAX_TOOL_TURNS + 1):
            try:
                turn = await self.llm.tool_turn(
                    system=MATCHING_SYSTEM, messages=messages, tools=tools.INVESTIGATION_TOOLS
                )
            except FixtureMissingError as exc:
                # Offline fake mode without a scripted investigation: proceed on
                # deterministic data alone, honestly noted in the trace.
                return {
                    "investigated": True,
                    "investigation_skipped": str(exc)[:200],
                    "event_summary": {"skipped": "no fixture"},
                }

            record_llm_call(
                self.session,
                invoice_id=state.invoice_id,
                purpose="matching",
                usage=turn.usage,
                model_id=turn.model_id,
                request_id=turn.request_id,
                trace_id=state.trace_id,
            )
            turns.append(
                {
                    "turn": turn_number,
                    "stop_reason": turn.stop_reason,
                    "tool_calls": [call["name"] for call in turn.tool_uses],
                }
            )
            messages.append({"role": "assistant", "content": turn.content})

            if turn.stop_reason == "tool_use":
                results = []
                for call in turn.tool_uses:
                    output = await self._execute_tool(state, call["name"], call["input"])
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": json.dumps(output, default=str),
                        }
                    )
                messages.append({"role": "user", "content": results})
                continue

            outcome = self._parse_outcome(turn.text)
            updates = self._apply_proposed_pairs(state, outcome)
            updates.update(
                {
                    "investigated": True,
                    "investigation_summary": outcome.summary or None,
                    "event_summary": {
                        "turns": turns,
                        "proposed_pairs": outcome.proposed_pairs,
                        "accepted_pairs": updates.pop("_accepted_pairs", 0),
                    },
                }
            )
            return updates

        return {
            "investigated": True,
            "investigation_skipped": "tool-turn limit reached",
            "event_summary": {"turns": turns, "skipped": "turn limit"},
        }

    def _investigation_brief(self, state: MatchingState) -> str:
        assert state.po is not None
        inv_by_index = {line.index: line for line in state.invoice_lines}
        po_by_index = {line.index: line for line in state.po.lines}
        payload = {
            "invoice": {
                "vendor_name": state.vendor_name,
                "invoice_number": state.invoice_number,
                "invoice_date": state.invoice_date,
                "currency": state.currency,
                "total": str(state.total),
            },
            "purchase_order": {
                "po_number": state.po.po_number,
                "order_date": state.po.order_date,
                "currency": state.po.currency,
                "total": str(state.po.total),
            },
            "open_discrepancies": [d.model_dump(exclude_none=True) for d in state.discrepancies],
            "unmatched_invoice_lines": [
                {
                    "index": index,
                    "description": inv_by_index[index].description,
                    "quantity": str(inv_by_index[index].quantity),
                    "unit_price": str(inv_by_index[index].unit_price),
                    "amount": str(inv_by_index[index].amount),
                }
                for index in state.unmatched_invoice
            ],
            "unmatched_po_lines": [
                {
                    "index": index,
                    "sku": po_by_index[index].sku,
                    "description": po_by_index[index].description,
                    "quantity": str(po_by_index[index].quantity),
                    "unit_price": str(po_by_index[index].unit_price),
                }
                for index in state.unmatched_po
            ],
            "received_by_po_line": {
                str(line_no): str(qty) for line_no, qty in state.received_by_po_line.items()
            },
        }
        return "Investigate the ambiguities in this invoice-to-PO match:\n\n" + json.dumps(
            payload, indent=2
        )

    async def _execute_tool(
        self, state: MatchingState, name: str, tool_input: dict[str, Any]
    ) -> Any:
        assert state.po is not None
        if name == "lookup_po":
            po = await tools.lookup_po(self.session, str(tool_input.get("po_number", "")))
            return po.model_dump(mode="json") if po else {"error": "purchase order not found"}
        if name == "lookup_receipts":
            po = await tools.lookup_po(self.session, str(tool_input.get("po_number", "")))
            if po is None:
                return {"error": "purchase order not found"}
            received = await tools.lookup_receipts(self.session, po.id)
            return {str(line_no): str(qty) for line_no, qty in received.items()}
        if name == "vendor_price_history":
            if state.vendor_id is None:
                return {"error": "invoice vendor is unknown"}
            return await tools.vendor_price_history(
                self.session,
                vendor_id=state.vendor_id,
                description=str(tool_input.get("description", "")),
                limit=int(tool_input.get("limit", 5)),
            )
        return {"error": f"unknown tool '{name}'"}

    @staticmethod
    def _parse_outcome(text: str) -> InvestigationOutcome:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return InvestigationOutcome.model_validate_json(text[start : end + 1])
            except Exception:  # fall back to summary-only below
                pass
        return InvestigationOutcome(proposed_pairs=[], summary=text.strip()[:1000])

    def _apply_proposed_pairs(
        self, state: MatchingState, outcome: InvestigationOutcome
    ) -> NodeUpdates:
        """Re-validate LLM-proposed pairings; a proposal is a hint, never a conclusion."""
        assert state.po is not None
        inv_by_index = {line.index: line for line in state.invoice_lines}
        po_by_index = {line.index: line for line in state.po.lines}
        alignment = list(state.alignment)
        unmatched_invoice = list(state.unmatched_invoice)
        unmatched_po = list(state.unmatched_po)
        accepted = 0

        for pair in outcome.proposed_pairs:
            if len(pair) != 2:
                continue
            inv_index, po_index = pair
            if inv_index not in unmatched_invoice or po_index not in unmatched_po:
                continue
            similarity = (
                fuzz.token_set_ratio(
                    inv_by_index[inv_index].description, po_by_index[po_index].description
                )
                / 100.0
            )
            if similarity < _MIN_PROPOSED_SIMILARITY:
                continue
            alignment.append(
                AlignedPair(
                    invoice_index=inv_index,
                    po_index=po_index,
                    similarity=round(similarity, 3),
                    source="llm_proposed",
                )
            )
            unmatched_invoice.remove(inv_index)
            unmatched_po.remove(po_index)
            accepted += 1

        return {
            "alignment": alignment,
            "unmatched_invoice": unmatched_invoice,
            "unmatched_po": unmatched_po,
            "_accepted_pairs": accepted,
        }

    # --- verdict (code only) -----------------------------------------------------

    async def assemble_verdict(self, state: MatchingState) -> NodeUpdates:
        if state.po is None:
            checks = MatchingChecks()
            discrepancies = [
                Discrepancy(
                    type="missing_po",
                    note=(
                        "no purchase order reference on the invoice"
                        if state.po_resolution == "no_po_number"
                        else f"purchase order '{state.po_number_raw}' not found"
                    ),
                )
            ]
            verdict = MatchingVerdict(
                invoice_id=str(state.invoice_id),
                verdict="exception",
                auto_approve_eligible=False,
                checks=checks,
                discrepancies=discrepancies,
                fraud_flags=state.fraud_flag_codes,
                agent_summary=(
                    f"No purchase order could be resolved for invoice "
                    f"{state.invoice_number or state.invoice_id} "
                    f"({state.po_resolution.replace('_', ' ')}). Human review required."
                ),
                trace_id=state.trace_id,
                po_number=state.po_number_raw,
                po_resolution=state.po_resolution,  # type: ignore[arg-type]
            )
            return {
                "verdict": verdict,
                "checks": checks,
                "discrepancies": discrepancies,
                "event_summary": {"verdict": "exception", "reason": "missing_po"},
            }

        # Three conditions beyond "nothing looks wrong", each of which was a way to
        # auto-approve on evidence the system doesn't actually have:
        #
        # - A fuzzy PO is a *guess* — matched by vendor, total and date window when
        #   the number the vendor printed didn't resolve. The verdict then reported
        #   the guessed PO number, so a reviewer couldn't even tell a substitution
        #   had happened. Guessing which authorisation to pay against is the one
        #   thing this system must not do silently.
        # - A closed PO is not an authorisation to spend. Its status was loaded into
        #   state and never read.
        # - A pairing the model proposed is a hint, not evidence. It is accepted at a
        #   *lower* similarity floor than the deterministic pass, so a line the code
        #   refused to pair could be paid on the model's say-so — with line
        #   descriptions coming from the invoice, which the vendor writes. The hint
        #   still earns its keep by explaining the mismatch to a human; it just can't
        #   be what releases the money.
        po_is_authoritative = state.po_resolution == "by_number" and state.po.status == "open"
        model_influenced = any(pair.source != "deterministic" for pair in state.alignment)
        eligible = (
            state.checks.all_pass()
            and not state.discrepancies
            and not state.blocking_fraud_codes
            and state.total <= self.settings.auto_approve_max_total
            and po_is_authoritative
            and not model_influenced
        )
        verdict_value = "auto_approve" if eligible else "exception"
        summary = state.investigation_summary or self._deterministic_summary(state, eligible)

        verdict = MatchingVerdict(
            invoice_id=str(state.invoice_id),
            verdict=verdict_value,  # type: ignore[arg-type]
            auto_approve_eligible=eligible,
            checks=state.checks,
            discrepancies=state.discrepancies,
            fraud_flags=state.fraud_flag_codes,
            agent_summary=summary,
            trace_id=state.trace_id,
            po_number=state.po.po_number,
            po_resolution=state.po_resolution,  # type: ignore[arg-type]
        )
        return {
            "verdict": verdict,
            "event_summary": {"verdict": verdict_value, "eligible": eligible},
        }

    def _deterministic_summary(self, state: MatchingState, eligible: bool) -> str:
        assert state.po is not None
        base = (
            f"Invoice {state.invoice_number or state.invoice_id} vs {state.po.po_number} "
            f"({state.po_resolution.replace('_', ' ')}): {len(state.alignment)} line(s) aligned."
        )
        if eligible:
            return (
                f"{base} Quantities, prices, and receipts all match; total "
                f"{state.total} {state.currency} is within the auto-approve threshold."
            )
        if not state.discrepancies:
            if state.total > self.settings.auto_approve_max_total:
                return (
                    f"{base} Clean match, but total {state.total} {state.currency} exceeds "
                    f"the auto-approve threshold "
                    f"({self.settings.auto_approve_max_total}); routed for human approval."
                )
            if state.blocking_fraud_codes:
                return (
                    f"{base} Clean match, but fraud screening flagged: "
                    f"{', '.join(state.blocking_fraud_codes)}."
                )
            failing = [k for k, v in state.checks.model_dump().items() if not v]
            return f"{base} Checks failing: {', '.join(failing)}."
        parts = []
        for d in state.discrepancies[:4]:
            impact = f" (impact {d.impact.amount} {d.impact.currency})" if d.impact else ""
            line = f" on '{d.line}'" if d.line else ""
            parts.append(f"{d.type}{line}{impact}")
        return f"{base} Discrepancies: {'; '.join(parts)}. Recommend review before approval."
