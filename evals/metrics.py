"""Field-level extraction metrics + validation/matching/dedup scoring.

Scoring rules:

* Scalar fields: normalized exact match (Decimal quantized, ISO dates, casefolded
  identifiers, exact currency); ``vendor_name`` counts as correct at token-set
  similarity ≥ 0.90.
* Null semantics: truth-present + wrong/missing prediction → FN (a wrong value also
  counts as FP); truth-absent + predicted value → FP; both absent → TN.
* Line items: optimal assignment (Hungarian) on description similarity; a predicted
  line is a TP only when all four fields match its aligned truth line.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

SCALAR_FIELDS = [
    "vendor_name",
    "vendor_tax_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "po_number",
    "subtotal",
    "tax_amount",
    "total",
    "payment_details_present",
]

_DECIMAL_FIELDS = {"subtotal", "tax_amount", "total"}
_FUZZY_FIELDS = {"vendor_name"}


@dataclass
class Tally:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, other: "Tally") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.tn += other.tn

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return 0.0 if (self.fp or self.fn) else None
        return 2 * p * r / (p + r)


def _values_equal(field_name: str, truth: Any, predicted: Any) -> bool:
    if field_name in _DECIMAL_FIELDS:
        try:
            return Decimal(str(truth)) == Decimal(str(predicted))
        except InvalidOperation:
            return False
    if field_name in _FUZZY_FIELDS:
        return fuzz.token_set_ratio(str(truth), str(predicted)) >= 90
    if isinstance(truth, bool) or isinstance(predicted, bool):
        return bool(truth) == bool(predicted)
    return str(truth).strip().casefold() == str(predicted).strip().casefold()


def score_scalar(field_name: str, truth: Any, predicted: Any) -> Tally:
    tally = Tally()
    truth_present = truth is not None and truth != ""
    pred_present = predicted is not None and predicted != ""
    if truth_present and pred_present:
        if _values_equal(field_name, truth, predicted):
            tally.tp += 1
        else:
            tally.fn += 1
            tally.fp += 1
    elif truth_present and not pred_present:
        tally.fn += 1
    elif pred_present and not truth_present:
        tally.fp += 1
    else:
        tally.tn += 1
    return tally


def _line_fields_match(truth: dict[str, Any], predicted: dict[str, Any]) -> int:
    matched = 0
    if fuzz.token_set_ratio(truth["description"], predicted["description"]) >= 90:
        matched += 1
    for key in ("quantity", "unit_price", "amount"):
        try:
            if Decimal(str(truth[key])) == Decimal(str(predicted[key])):
                matched += 1
        except (InvalidOperation, KeyError):
            pass
    return matched


def score_line_items(
    truth_lines: list[dict[str, Any]], predicted_lines: list[dict[str, Any]]
) -> tuple[Tally, float | None]:
    """Line-level tally (TP = aligned pair with all 4 fields correct) plus
    field-accuracy within aligned pairs."""
    tally = Tally()
    if not truth_lines and not predicted_lines:
        tally.tn += 1
        return tally, None
    if not predicted_lines:
        tally.fn += len(truth_lines)
        return tally, None
    if not truth_lines:
        tally.fp += len(predicted_lines)
        return tally, None

    cost = [
        [
            1.0 - fuzz.token_set_ratio(t["description"], p["description"]) / 100.0
            for p in predicted_lines
        ]
        for t in truth_lines
    ]
    rows, cols = linear_sum_assignment(cost)
    aligned: set[tuple[int, int]] = set()
    field_hits = 0
    for row, col in zip(rows, cols, strict=True):
        if 1.0 - cost[row][col] < 0.4:
            continue  # implausible pairing — treat both sides as unmatched
        aligned.add((row, col))
        hits = _line_fields_match(truth_lines[row], predicted_lines[col])
        field_hits += hits
        if hits == 4:
            tally.tp += 1
        else:
            tally.fn += 1
            tally.fp += 1
    tally.fn += len(truth_lines) - len(aligned)
    tally.fp += len(predicted_lines) - len(aligned)
    accuracy = field_hits / (len(aligned) * 4) if aligned else 0.0
    return tally, accuracy


@dataclass
class ExtractionScores:
    per_field: dict[str, Tally] = field(default_factory=dict)
    line_items: Tally = field(default_factory=Tally)
    line_field_accuracies: list[float] = field(default_factory=list)
    doc_exact: int = 0
    docs: int = 0
    failed_docs: int = 0

    def score_doc(self, truth: dict[str, Any], predicted: dict[str, Any] | None) -> None:
        self.docs += 1
        if predicted is None:
            self.failed_docs += 1
            for name in SCALAR_FIELDS:
                tally = self.per_field.setdefault(name, Tally())
                if truth.get(name) is not None:
                    tally.fn += 1
            self.line_items.fn += len(truth.get("line_items", []))
            return

        all_scalars_ok = True
        for name in SCALAR_FIELDS:
            outcome = score_scalar(name, truth.get(name), predicted.get(name))
            self.per_field.setdefault(name, Tally()).add(outcome)
            if outcome.fn or outcome.fp:
                all_scalars_ok = False

        line_tally, accuracy = score_line_items(
            truth.get("line_items", []), predicted.get("line_items", [])
        )
        self.line_items.add(line_tally)
        if accuracy is not None:
            self.line_field_accuracies.append(accuracy)
        lines_perfect = line_tally.fp == 0 and line_tally.fn == 0
        if all_scalars_ok and lines_perfect:
            self.doc_exact += 1

    def summary(self) -> dict[str, Any]:
        def rounded(value: float | None) -> float | None:
            return round(value, 4) if value is not None else None

        totals_f1 = [
            self.per_field[name].f1
            for name in ("subtotal", "tax_amount", "total")
            if name in self.per_field and self.per_field[name].f1 is not None
        ]
        return {
            "docs": self.docs,
            "extraction_failed_docs": self.failed_docs,
            "doc_exact_match": rounded(self.doc_exact / self.docs) if self.docs else None,
            "totals_f1": rounded(sum(totals_f1) / len(totals_f1)) if totals_f1 else None,
            "line_items_f1": rounded(self.line_items.f1),
            "line_field_accuracy": rounded(
                sum(self.line_field_accuracies) / len(self.line_field_accuracies)
            )
            if self.line_field_accuracies
            else None,
            "per_field_f1": {
                name: rounded(tally.f1) for name, tally in sorted(self.per_field.items())
            },
        }
