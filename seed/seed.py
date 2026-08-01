"""Seed demo data: an API key, vendors, purchase orders, and receiving records.

Idempotent — safe to re-run.

    uv run python -m seed.seed [--api-key <literal key to install>]
"""

import argparse
import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.core.db import dispose_engine, get_sessionmaker
from app.core.money import quantize_amount
from app.core.normalize import normalize_vendor_name
from app.core.security import generate_api_key, hash_api_key
from app.models import ApiKey, PoLineItem, PurchaseOrder, Receipt, ReceiptLineItem, Vendor

# Deliberately every scope: the demo walks the entire pipeline including review
# and export. Real keys are minted with `ingest,read` and widened on request.
DEMO_SCOPES = ["ingest", "read", "review", "export"]

# Matches the demo invoice fixtures in seed/fixtures/llm/demo.json.
DEMO_VENDORS = [
    "ACME Office Supply Co",
    "Globex Catering Ltd",
    "Initech Software GmbH",
    "Umbrella Logistics Inc",
]

DEMO_POS = [
    {
        "po_number": "PO-2231",
        "vendor": "ACME Office Supply Co",
        "order_date": date(2026, 6, 20),
        "lines": [
            ("WID-A200", "Widget A-200", "10", "47.00"),
            ("PPR-A4", "A4 copy paper, 80gsm, 500 sheets", "20", "6.25"),
        ],
        "received": {1: "10", 2: "20"},
    },
    {
        "po_number": "PO-2232",
        "vendor": "Globex Catering Ltd",
        "order_date": date(2026, 6, 22),
        "lines": [("CTR-LUNCH", "Team lunch catering (per head)", "40", "18.50")],
        "received": {1: "40"},
    },
    {
        "po_number": "PO-2233",
        "vendor": "Initech Software GmbH",
        "order_date": date(2026, 6, 25),
        "lines": [("SFT-LIC", "Software licence, annual seat", "12", "180.00")],
        "received": {1: "12"},
    },
]


async def seed(api_key_literal: str | None) -> str | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Vendors
        for name in DEMO_VENDORS:
            normalized = normalize_vendor_name(name)
            existing = await session.scalar(
                select(Vendor).where(Vendor.normalized_name == normalized)
            )
            if existing is None:
                session.add(Vendor(name=name, normalized_name=normalized, default_currency="USD"))
        await session.flush()

        # Purchase orders + receiving records
        for spec in DEMO_POS:
            existing_po = await session.scalar(
                select(PurchaseOrder).where(PurchaseOrder.po_number == spec["po_number"])
            )
            if existing_po is not None:
                continue
            vendor = await session.scalar(
                select(Vendor).where(
                    Vendor.normalized_name == normalize_vendor_name(str(spec["vendor"]))
                )
            )
            assert vendor is not None
            po = PurchaseOrder(
                po_number=str(spec["po_number"]),
                vendor_id=vendor.id,
                currency="USD",
                total=Decimal(0),
                order_date=spec["order_date"],  # type: ignore[arg-type]
            )
            session.add(po)
            await session.flush()

            total = Decimal(0)
            for line_no, (sku, description, quantity, price) in enumerate(
                spec["lines"],
                start=1,  # type: ignore[arg-type]
            ):
                amount = quantize_amount(Decimal(quantity) * Decimal(price), "USD")
                total += amount
                session.add(
                    PoLineItem(
                        po_id=po.id,
                        line_no=line_no,
                        sku=sku,
                        description=description,
                        quantity=Decimal(quantity),
                        unit_price=Decimal(price),
                        amount=amount,
                    )
                )
            po.total = quantize_amount(total, "USD")

            receipt = Receipt(
                receipt_number=f"RCV-{spec['po_number']}",
                po_id=po.id,
                received_date=spec["order_date"],  # type: ignore[arg-type]
            )
            session.add(receipt)
            await session.flush()
            for line_no, quantity in spec["received"].items():  # type: ignore[union-attr]
                session.add(
                    ReceiptLineItem(
                        receipt_id=receipt.id,
                        po_line_no=int(line_no),
                        quantity_received=Decimal(quantity),
                    )
                )

        # Demo API key
        raw_key = api_key_literal
        if raw_key is not None:
            existing_key = await session.scalar(
                select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key))
            )
            if existing_key is None:
                session.add(
                    ApiKey(
                        name="demo",
                        prefix=raw_key[:12],
                        key_hash=hash_api_key(raw_key),
                        scopes=DEMO_SCOPES,
                    )
                )
        else:
            existing_demo = await session.scalar(select(ApiKey).where(ApiKey.name == "demo"))
            if existing_demo is None:
                generated = generate_api_key("demo")
                session.add(
                    ApiKey(
                        name="demo",
                        prefix=generated.prefix,
                        key_hash=generated.key_hash,
                        scopes=DEMO_SCOPES,
                    )
                )
                raw_key = generated.full_key

        await session.commit()
    return raw_key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default=None, help="Install this literal API key")
    args = parser.parse_args()

    async def run() -> str | None:
        try:
            return await seed(args.api_key)
        finally:
            await dispose_engine()

    key = asyncio.run(run())
    print(f"Seeded {len(DEMO_VENDORS)} vendors, {len(DEMO_POS)} purchase orders + receipts.")
    if key:
        print(f"Demo API key: {key}")
        Path("evals/output").mkdir(parents=True, exist_ok=True)
        Path("evals/output/demo_key.json").write_text(json.dumps({"api_key": key}) + "\n")
    else:
        print("Demo API key already exists (use `python -m app.cli keys create` for a new one).")


if __name__ == "__main__":
    main()
