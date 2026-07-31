"""Promote reviewer corrections into the eval dataset.

This closes the product's central loop: a human fixes a field in the review queue,
that correction is stored as ground truth, and the corrected invoice becomes a
labeled eval case. Real documents the system got wrong are exactly the cases worth
measuring against.

    uv run python -m evals.promote_corrections --dataset v1 [--since 2026-07-01] [--dry-run]

Cases are written as ``corrected_<invoice_id8>`` with ``provenance:
"reviewer_correction"`` in meta, so metrics can be sliced synthetic vs. real.
"""

import argparse
import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import dispose_engine, get_sessionmaker
from app.models import FieldCorrection, Invoice, InvoiceFile


def _truth_from_invoice(invoice: Invoice) -> dict[str, Any]:
    """Post-correction values are the ground truth: a human verified them."""
    return {
        "vendor_name": invoice.vendor_name,
        "vendor_tax_id": (invoice.extraction or {}).get("vendor_tax_id"),
        "invoice_number": invoice.invoice_number,
        "invoice_date": str(invoice.invoice_date) if invoice.invoice_date else None,
        "due_date": str(invoice.due_date) if invoice.due_date else None,
        "currency": invoice.currency,
        "po_number": invoice.po_number,
        "line_items": [
            {
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "amount": str(line.amount),
            }
            for line in invoice.line_items
        ],
        "subtotal": str(invoice.subtotal) if invoice.subtotal is not None else None,
        "tax_amount": str(invoice.tax_amount) if invoice.tax_amount is not None else None,
        "total": str(invoice.total) if invoice.total is not None else None,
        "payment_details_present": invoice.payment_details_present,
    }


async def promote(dataset: str, since: datetime | None, dry_run: bool) -> int:
    out_dir = Path("evals/dataset") / dataset
    cases_dir = out_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    promoted = 0
    async with get_sessionmaker()() as session:
        query = select(FieldCorrection)
        if since is not None:
            query = query.where(FieldCorrection.created_at >= since)
        corrections = (await session.scalars(query)).all()

        by_invoice: dict[Any, list[FieldCorrection]] = {}
        for correction in corrections:
            by_invoice.setdefault(correction.invoice_id, []).append(correction)

        for invoice_id, invoice_corrections in by_invoice.items():
            invoice = await session.scalar(
                select(Invoice)
                .where(Invoice.id == invoice_id)
                .options(selectinload(Invoice.line_items), selectinload(Invoice.file))
            )
            if invoice is None or invoice.extraction is None:
                continue

            case_id = f"corrected_{str(invoice_id)[:8]}"
            case_dir = cases_dir / case_id
            truth = _truth_from_invoice(invoice)
            meta = {
                "case_id": case_id,
                "category": "extraction",
                "template": "real_document",
                "provenance": "reviewer_correction",
                "source_invoice_id": str(invoice_id),
                "corrected_fields": sorted({c.field_path for c in invoice_corrections}),
                "corrections": [
                    {
                        "field_path": c.field_path,
                        "extracted": c.old_value,
                        "corrected": c.new_value,
                    }
                    for c in invoice_corrections
                ],
                "promoted_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }

            if dry_run:
                print(f"[dry-run] {case_id}: would promote {meta['corrected_fields']}")
                promoted += 1
                continue

            case_dir.mkdir(parents=True, exist_ok=True)
            (case_dir / "truth.json").write_text(json.dumps(truth, indent=2) + "\n")
            (case_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

            # Copy the source document so the case is reproducible on its own.
            source: InvoiceFile | None = invoice.file
            if source is not None and Path(source.storage_path).is_file():
                shutil.copyfile(source.storage_path, case_dir / "invoice.pdf")
            promoted += 1
            print(f"{case_id}: promoted (corrected {', '.join(meta['corrected_fields'])})")

    if promoted and not dry_run:
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        manifest["promoted_corrections"] = manifest.get("promoted_corrections", 0) + promoted
        manifest["last_promotion_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return promoted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="v1")
    parser.add_argument("--since", default=None, help="ISO date, e.g. 2026-07-01")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since else None

    async def run() -> int:
        try:
            return await promote(args.dataset, since, args.dry_run)
        finally:
            await dispose_engine()

    count = asyncio.run(run())
    print(f"\n{count} corrected invoice(s) {'would be ' if args.dry_run else ''}promoted.")
    if count and not args.dry_run:
        print(
            "Re-run the eval harness to measure against them:\n"
            f"  uv run python -m evals.run --mode live --config validation "
            f"--dataset {args.dataset}"
        )


if __name__ == "__main__":
    main()
