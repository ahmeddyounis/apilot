"""Synthetic invoice case construction.

Values are built so the arithmetic is exact *by construction* (qty * price is
quantized to the currency, subtotal is the true sum, total = subtotal + tax) —
ground truth is perfect and free. Corruption/discrepancy cases then break specific
invariants on purpose and record what the system is expected to catch.
"""

import json
import random
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from faker import Faker

from app.core.money import quantize_amount

TEMPLATES = ["clean_modern", "dense_table", "serif_letterhead", "minimal", "euro_vat"]

_PRODUCTS = [
    ("WID-A200", "Widget A-200"),
    ("WID-B100", "Widget B-100 (compact)"),
    ("PPR-A4", "A4 copy paper, 80gsm, 500 sheets"),
    ("TNR-55X", "Toner cartridge 55X black"),
    ("CBL-USB2", "USB-C cable 2m braided"),
    ("SRV-CONS", "Consulting services (hourly)"),
    ("SFT-LIC", "Software licence, annual seat"),
    ("SHP-STD", "Standard shipping & handling"),
    ("CHR-ERGO", "Ergonomic task chair"),
    ("DSK-LED", "LED desk lamp, dimmable"),
]

_TAX_RATES = [Decimal("0.00"), Decimal("0.05"), Decimal("0.08"), Decimal("0.19"), Decimal("0.20")]


@dataclass
class Case:
    case_id: str
    category: str  # extraction | corruption | matching | dedup
    template: str
    truth: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)
    render_seed: int = 0


def _money(rng: random.Random, low: float, high: float) -> Decimal:
    cents = rng.randint(int(low * 100), int(high * 100))
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def _build_invoice(
    rng: random.Random,
    faker: Faker,
    *,
    currency: str = "USD",
    n_lines: int | None = None,
    vendor_name: str | None = None,
    po_number: str | None = None,
    invoice_number: str | None = None,
    small: bool = False,
) -> dict[str, Any]:
    """``small=True`` keeps the total under a typical auto-approve threshold, so
    matching cases can actually exercise the auto-approve path."""
    vendor = vendor_name or f"{faker.company()}"
    n = n_lines or rng.randint(1, 5)
    lines = []
    subtotal = Decimal(0)
    products = rng.sample(_PRODUCTS, k=min(n, len(_PRODUCTS)))
    for sku, description in products:
        quantity = Decimal(rng.randint(1, 4) if small else rng.randint(1, 24))
        unit_price = _money(rng, 3, 90) if small else _money(rng, 3, 400)
        amount = quantize_amount(quantity * unit_price, currency)
        subtotal += amount
        lines.append(
            {
                "sku": sku,
                "description": description,
                "quantity": str(quantity),
                "unit_price": str(unit_price),
                "amount": str(amount),
            }
        )
    subtotal = quantize_amount(subtotal, currency)
    tax_rate = rng.choice(_TAX_RATES)
    tax = quantize_amount(subtotal * tax_rate, currency)
    total = quantize_amount(subtotal + tax, currency)

    year = 2026
    month = rng.randint(1, 6)
    day = rng.randint(1, 28)
    invoice_date = f"{year}-{month:02d}-{day:02d}"
    due_day = min(day + rng.choice([14, 30]), 28)
    due_month = month + 1 if day + 30 > 28 else month
    due_date = f"{year}-{min(due_month, 12):02d}-{due_day:02d}"

    return {
        "vendor_name": vendor,
        "vendor_tax_id": faker.bothify(text="??-#######").upper() if rng.random() < 0.6 else None,
        "invoice_number": invoice_number or f"INV-{year}-{rng.randint(1000, 9999)}",
        "invoice_date": invoice_date,
        "due_date": due_date,
        "currency": currency,
        "po_number": po_number,
        "line_items": lines,
        "subtotal": str(subtotal),
        "tax_amount": str(tax),
        "total": str(total),
        "payment_details_present": rng.random() < 0.4,
        "tax_rate_display": f"{(tax_rate * 100):.0f}%",
    }


_CORRUPTIONS = [
    # (name, expected hard rule ids, mutator)
    ("wrong_total", ["V-003"], lambda t: t.update(total=_bump(t["total"], "37.11"))),
    ("line_sum_off", ["V-002"], lambda t: t.update(subtotal=_bump(t["subtotal"], "25.00"))),
    (
        "bad_line_math",
        ["V-001", "V-002"],
        lambda t: t["line_items"][0].update(amount=_bump(t["line_items"][0]["amount"], "13.37")),
    ),
    ("impossible_date", ["V-008"], lambda t: t.update(invoice_date="2031-01-15")),
    ("due_before_invoice", ["V-009"], lambda t: t.update(due_date="2025-01-01")),
    (
        "negative_qty",
        ["V-001", "V-006"],
        lambda t: t["line_items"][0].update(quantity="-2"),
    ),
]


def _bump(value: str, delta: str) -> str:
    return str(Decimal(value) + Decimal(delta))


def _matching_cases(rng: random.Random, faker: Faker) -> list[Case]:
    """Cases carrying PO/receipt seed data + an expected matching outcome."""
    cases = []
    scenarios = [
        ("match_clean", "auto_approve", []),
        ("match_qty_over", "exception", ["quantity_mismatch"]),
        ("match_price_up", "exception", ["price_mismatch"]),
        ("match_missing_po", "exception", ["missing_po"]),
    ]
    for index, (name, expected_verdict, expected_types) in enumerate(scenarios):
        vendor = f"{faker.company()}"
        po_number = f"PO-{7000 + index}"
        truth = _build_invoice(
            rng,
            faker,
            n_lines=2,
            vendor_name=vendor,
            po_number="PO-0000" if name == "match_missing_po" else po_number,
            small=True,
        )
        po_lines = [
            {
                "line_no": i + 1,
                "sku": line["sku"],
                "description": line["description"],
                "quantity": line["quantity"],
                "unit_price": line["unit_price"],
            }
            for i, line in enumerate(truth["line_items"])
        ]
        if name == "match_qty_over":
            po_lines[0]["quantity"] = str(Decimal(po_lines[0]["quantity"]) - 1)
        if name == "match_price_up":
            po_lines[0]["unit_price"] = str(
                quantize_amount(Decimal(po_lines[0]["unit_price"]) * Decimal("0.90"), "USD")
            )
        cases.append(
            Case(
                case_id=name,
                category="matching",
                template=rng.choice(TEMPLATES[:4]),
                truth=truth,
                meta={
                    "expected_verdict": expected_verdict,
                    "expected_discrepancy_types": expected_types,
                    "po_seed": None
                    if name == "match_missing_po"
                    else {
                        "po_number": po_number,
                        "vendor_name": vendor,
                        "currency": "USD",
                        "order_date": "2026-01-02",
                        "line_items": po_lines,
                        "receive_in_full": True,
                    },
                },
            )
        )
    return cases


def build_dataset(master_seed: int = 42) -> list[Case]:
    rng = random.Random(master_seed)
    faker = Faker()
    faker.seed_instance(master_seed)
    cases: list[Case] = []

    # 10 clean extraction cases across the template families (2 in EUR format).
    for index in range(10):
        template = TEMPLATES[index % len(TEMPLATES)]
        currency = "EUR" if template == "euro_vat" else "USD"
        truth = _build_invoice(rng, faker, currency=currency)
        cases.append(
            Case(
                case_id=f"clean_{index:02d}_{template}",
                category="extraction",
                template=template,
                truth=truth,
                meta={},
                render_seed=master_seed + index,
            )
        )

    # 6 corruption cases: the printed document itself is wrong.
    for name, expected_rules, mutate in _CORRUPTIONS:
        truth = _build_invoice(rng, faker, n_lines=3)
        mutate(truth)
        cases.append(
            Case(
                case_id=f"corrupt_{name}",
                category="corruption",
                template=rng.choice(TEMPLATES),
                truth=truth,
                meta={"corruption": name, "expected_rule_ids": expected_rules},
            )
        )

    cases.extend(_matching_cases(rng, faker))

    # 2 dedup cases: an exact resubmission and a re-billed near-duplicate.
    base = _build_invoice(rng, faker, n_lines=2)
    cases.append(
        Case(
            case_id="dedup_original",
            category="dedup",
            template="clean_modern",
            truth=base,
            meta={"dedup_role": "original"},
        )
    )
    resub = json.loads(json.dumps(base))
    resub["invoice_number"] = base["invoice_number"] + "-R"
    cases.append(
        Case(
            case_id="dedup_near_resub",
            category="dedup",
            template="clean_modern",
            truth=resub,
            meta={"dedup_role": "near_duplicate", "expected_flag": "DUP-SUSPECT"},
        )
    )
    return cases


def write_dataset(cases: list[Case], out_dir: Path, *, master_seed: int) -> None:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1
        case_dir = out_dir / "cases" / case.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        truth = {k: v for k, v in case.truth.items() if k != "tax_rate_display"}
        (case_dir / "truth.json").write_text(json.dumps(truth, indent=2) + "\n")
        meta = {
            "case_id": case.case_id,
            "category": case.category,
            "template": case.template,
            "provenance": "synthetic",
            **case.meta,
        }
        (case_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    manifest = {
        "dataset_version": out_dir.name,
        "master_seed": master_seed,
        "counts_by_category": counts,
        "total_cases": len(cases),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
