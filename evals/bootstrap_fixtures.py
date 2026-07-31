"""Bootstrap FakeLLM fixtures for the eval dataset, derived from ground truth.

Until a live recording session replaces them (`evals.run --mode live --record`,
pending an API key), fixture-mode evals replay these truth-derived outputs. They are
deliberately not all perfect: the naive config gets fenced/broken JSON (showing why
structured outputs exist) and schema_retry gets one scripted garbled-then-correct
sequence (exercising retry-with-feedback). Corruption cases carry the corrupt
document values verbatim — a faithful extractor transcribes what is printed, and
catching it is the validation engine's job.

    uv run python -m evals.bootstrap_fixtures --dataset v1
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

IBAN_LINE = "Remit to: IBAN DE89 3704 0044 0532 0130 00 · BIC COBADEFFXXX"

_CONF_FIELDS = [
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
    "line_items",
    "payment_details_present",
]


def _confidence(case_id: str, field: str) -> float:
    digest = hashlib.sha256(f"{case_id}:{field}".encode()).digest()
    return round(0.90 + (digest[0] / 255) * 0.09, 2)


def truth_to_wire(case_id: str, truth: dict[str, Any]) -> dict[str, Any]:
    present = [f for f in _CONF_FIELDS if truth.get(f) is not None or f == "line_items"]
    return {
        "vendor_name": truth["vendor_name"],
        "vendor_tax_id": truth.get("vendor_tax_id"),
        "invoice_number": truth["invoice_number"],
        "invoice_date": truth["invoice_date"],
        "due_date": truth.get("due_date"),
        "currency": truth["currency"],
        "po_number": truth.get("po_number"),
        "line_items": [
            {
                "description": line["description"],
                "quantity": line["quantity"],
                "unit_price": line["unit_price"],
                "amount": line["amount"],
            }
            for line in truth["line_items"]
        ],
        "subtotal": truth["subtotal"],
        "tax_amount": truth["tax_amount"],
        "total": truth["total"],
        "payment_details_present": bool(truth.get("payment_details_present")),
        "payment_details_raw": IBAN_LINE if truth.get("payment_details_present") else None,
        "field_confidence": {f: _confidence(case_id, f) for f in present},
        "source_page_refs": dict.fromkeys(present, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="v1")
    args = parser.parse_args()

    cases_dir = Path("evals/dataset") / args.dataset / "cases"
    fixtures: dict[str, Any] = {}
    case_ids = sorted(path.name for path in cases_dir.iterdir() if path.is_dir())

    for index, case_id in enumerate(case_ids):
        truth = json.loads((cases_dir / case_id / "truth.json").read_text())
        wire = truth_to_wire(case_id, truth)

        # schema_retry / validation: perfect wires; one scripted retry sequence.
        record: Any = {"wire": wire}
        if index == 0:
            garbled = dict(wire)
            garbled["invoice_date"] = "not-a-date"
            record = [{"wire": garbled}, {"wire": wire}]
        fixtures[f"eval:schema_retry:{case_id}"] = record
        fixtures[f"eval:validation:{case_id}"] = {"wire": wire}

        # naive: fenced JSON text; two cases degrade to prose (no parseable JSON).
        if index in (1, 7):
            fixtures[f"eval:naive:{case_id}"] = {
                "text": (
                    f"This looks like an invoice from {truth['vendor_name']} for a "
                    f"total of {truth['total']} {truth['currency']}. Let me know if "
                    "you need specific fields!"
                )
            }
        else:
            fixtures[f"eval:naive:{case_id}"] = {
                "text": "Here is the extracted data:\n```json\n"
                + json.dumps(wire, indent=1)
                + "\n```"
            }

    out = Path("evals/fixtures/bootstrap.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixtures, indent=1) + "\n")
    print(f"wrote {len(fixtures)} fixtures for {len(case_ids)} cases → {out}")


if __name__ == "__main__":
    main()
