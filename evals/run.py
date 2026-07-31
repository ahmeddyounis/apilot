"""Eval harness runner.

    uv run python -m evals.run --mode fixture --config schema_retry --dataset v1 [--check]

Configurations are the real production code paths (the README ladder):

    naive        plain prompt, best-effort JSON, no retry, no validation gating
    schema_retry structured outputs + retry-with-feedback
    validation   + deterministic rules engine, screening, matching (full pipeline)
    paged        + page-by-page extraction (live mode only — needs real multi-page PDFs)

Fixture mode replays recorded/bootstrapped model outputs through the REAL services
against an ephemeral Postgres container — zero secrets, CI-safe. Live mode calls the
Anthropic API (and can --record fixtures for future replays).
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

REPORTS_DIR = Path("evals/reports")
THRESHOLDS_PATH = Path("evals/thresholds.json")
BASELINE_PATH = Path("evals/baseline_metrics.json")

CONFIGS = ("naive", "schema_retry", "validation", "paged")


def _load_cases(dataset: str) -> list[dict[str, Any]]:
    root = Path("evals/dataset") / dataset / "cases"
    cases = []
    for case_dir in sorted(root.iterdir()):
        if not case_dir.is_dir():
            continue
        cases.append(
            {
                "case_id": case_dir.name,
                "truth": json.loads((case_dir / "truth.json").read_text()),
                "meta": json.loads((case_dir / "meta.json").read_text()),
                "pdf_path": case_dir / "invoice.pdf",
            }
        )
    # Dedup originals must be ingested before their resubmissions.
    cases.sort(key=lambda c: (c["meta"].get("dedup_role") == "near_duplicate", c["case_id"]))
    return cases


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        "pgvector/pgvector:pg17", username="apilot", password="apilot", dbname="apilot"
    ) as pg:
        db_url = (
            f"postgresql+asyncpg://apilot:apilot@{pg.get_container_host_ip()}"
            f":{pg.get_exposed_port(5432)}/apilot"
        )
        os.environ["APILOT_DATABASE_URL"] = db_url
        storage_dir = Path("evals/output/storage")
        storage_dir.mkdir(parents=True, exist_ok=True)
        os.environ["APILOT_STORAGE_DIR"] = str(storage_dir)
        if args.mode == "fixture":
            os.environ["APILOT_LLM_MODE"] = "fake"

        from app.core.config import get_settings
        from app.core.db import dispose_engine

        get_settings.cache_clear()
        await dispose_engine()

        from alembic.config import Config

        from alembic import command

        # alembic/env.py calls asyncio.run(), which cannot nest inside this loop.
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")

        try:
            return await _run_cases(args)
        finally:
            await dispose_engine()


async def _run_cases(args: argparse.Namespace) -> dict[str, Any]:
    from sqlalchemy import select

    from app.agent import graph as agent_graph  # noqa: F401 — ensure import side effects
    from app.api.deps import AuthContext
    from app.core.config import get_settings
    from app.core.db import get_sessionmaker
    from app.llm.embeddings import build_embedding_provider
    from app.llm.factory import build_llm_client
    from app.llm.fake import scenario
    from app.models import Invoice, InvoiceFile, InvoiceStatus, MatchingResult
    from app.schemas.po import POCreateIn
    from app.services.dedup import run_screening_stage
    from app.services.extraction import ExtractionConfig, run_extraction
    from app.services.matching import run_matching_stage
    from app.services.purchase_orders import create_purchase_order, create_receipt
    from app.services.state_machine import transition
    from app.services.validation import run_validation_stage
    from evals.generator.render import render_minimal_pdf
    from evals.metrics import ExtractionScores

    settings = get_settings()
    llm = build_llm_client(settings, model=args.model or settings.extraction_model)
    embedder = build_embedding_provider(settings)
    auth = AuthContext(key_id=uuid4(), name="eval", scopes=frozenset({"ingest"}))

    extraction_config = {
        "naive": ExtractionConfig(structured_outputs=False, retry_with_feedback=False),
        "schema_retry": ExtractionConfig(),
        "validation": ExtractionConfig(),
        "paged": ExtractionConfig(force_paged=True),
    }[args.config]
    full_pipeline = args.config in ("validation", "paged")

    cases = _load_cases(args.dataset)
    scores = ExtractionScores()
    catch_rates: list[float] = []
    matching_total = matching_correct = 0
    discrepancy_correct = 0
    dedup_expected = dedup_caught = false_dup_holds = 0
    llm_cost = Decimal(0)

    sessionmaker = get_sessionmaker()
    for case in cases:
        case_id, truth, meta = case["case_id"], case["truth"], case["meta"]
        async with sessionmaker() as session:
            # Seed PO/receipt data for matching cases before the pipeline runs.
            if full_pipeline and meta.get("po_seed"):
                seed = meta["po_seed"]
                po_payload = POCreateIn.model_validate(
                    {k: v for k, v in seed.items() if k != "receive_in_full"}
                )
                po = await create_purchase_order(session, po_payload, auth=auth)
                if seed.get("receive_in_full"):
                    from app.schemas.po import ReceiptCreateIn

                    await create_receipt(
                        session,
                        ReceiptCreateIn.model_validate(
                            {
                                "receipt_number": f"RCV-{seed['po_number']}",
                                "po_number": seed["po_number"],
                                "received_date": seed["order_date"],
                                "line_items": [
                                    {
                                        "po_line_no": line["line_no"],
                                        "quantity_received": line["quantity"],
                                    }
                                    for line in seed["line_items"]
                                ],
                            }
                        ),
                        auth=auth,
                    )
                _ = po

            pdf_bytes = (
                case["pdf_path"].read_bytes()
                if case["pdf_path"].exists()
                else render_minimal_pdf(case_id)
            )
            if args.mode == "live" and not case["pdf_path"].exists():
                raise SystemExit(
                    f"live mode requires rendered PDFs; run "
                    f"`uv run python -m evals.generator --dataset {args.dataset} --render html`"
                )

            storage_path = Path(os.environ["APILOT_STORAGE_DIR"]) / f"{case_id}.pdf"
            storage_path.write_bytes(pdf_bytes)
            file_row = InvoiceFile(
                sha256=f"{uuid4().hex}{uuid4().hex}"[:64],
                mime="application/pdf",
                size_bytes=len(pdf_bytes),
                storage_path=str(storage_path),
                original_filename=f"{case_id}.pdf",
            )
            session.add(file_row)
            await session.flush()
            invoice = Invoice(status=InvoiceStatus.RECEIVED, file_id=file_row.id)
            session.add(invoice)
            await session.flush()
            await session.refresh(invoice, attribute_names=["file"])
            await transition(session, invoice, InvoiceStatus.EXTRACTING, actor="eval")
            await session.commit()

            key = f"eval:{args.config}:{case_id}"
            if args.mode == "fixture":
                with scenario(key):
                    await run_extraction(
                        session, invoice, llm=llm, settings=settings, config=extraction_config
                    )
            else:
                await run_extraction(
                    session, invoice, llm=llm, settings=settings, config=extraction_config
                )

            predicted = None
            if invoice.status is InvoiceStatus.EXTRACTED:
                await session.refresh(invoice, attribute_names=["line_items"])
                predicted = {
                    "vendor_name": invoice.vendor_name,
                    "vendor_tax_id": (invoice.extraction or {}).get("vendor_tax_id"),
                    "invoice_number": invoice.invoice_number,
                    "invoice_date": str(invoice.invoice_date),
                    "due_date": str(invoice.due_date) if invoice.due_date else None,
                    "currency": invoice.currency,
                    "po_number": invoice.po_number,
                    "subtotal": str(invoice.subtotal),
                    "tax_amount": str(invoice.tax_amount),
                    "total": str(invoice.total),
                    "payment_details_present": invoice.payment_details_present,
                    "line_items": [
                        {
                            "description": line.description,
                            "quantity": str(line.quantity),
                            "unit_price": str(line.unit_price),
                            "amount": str(line.amount),
                        }
                        for line in invoice.line_items
                    ],
                }
            scores.score_doc(truth, predicted)

            # Seeded-corruption catch rate is measured for EVERY config. Without the
            # validation layer the only thing that can catch a corrupt document is
            # extraction failing outright — which is exactly the point of the ladder.
            if meta.get("expected_rule_ids") and not full_pipeline:
                caught = invoice.status is InvoiceStatus.EXTRACTION_FAILED
                catch_rates.append(1.0 if caught else 0.0)

            # Full-pipeline stages: validation gate, screening, matching.
            if full_pipeline and invoice.status is InvoiceStatus.EXTRACTED:
                passed = await run_validation_stage(session, invoice, settings=settings)
                if meta.get("expected_rule_ids"):
                    fired = {
                        f["rule_id"] for f in (invoice.validation_results or {}).get("findings", [])
                    }
                    expected = set(meta["expected_rule_ids"])
                    catch_rates.append(len(fired & expected) / len(expected))
                if passed:
                    proceed = await run_screening_stage(
                        session, invoice, settings=settings, embedder=embedder
                    )
                    if invoice.status is InvoiceStatus.HELD and not meta.get("dedup_role"):
                        false_dup_holds += 1
                    if proceed:
                        await transition(session, invoice, InvoiceStatus.MATCHING, actor="eval")
                        await session.commit()
                        with scenario(f"{key}:match"):
                            await run_matching_stage(session, invoice, llm=llm, settings=settings)
            elif meta.get("expected_rule_ids") and full_pipeline:
                # Extraction failed before validation could judge the document; the
                # corruption is still caught (nothing reached the approval path).
                catch_rates.append(1.0)

            # Matching outcome scoring.
            if full_pipeline and meta.get("expected_verdict"):
                matching_total += 1
                result = await session.scalar(
                    select(MatchingResult)
                    .where(MatchingResult.invoice_id == invoice.id)
                    .order_by(MatchingResult.created_at.desc())
                    .limit(1)
                )
                verdict = (result.verdict if result else {}) or {}
                if verdict.get("verdict") == meta["expected_verdict"]:
                    matching_correct += 1
                found_types = {d["type"] for d in verdict.get("discrepancies", [])}
                if set(meta.get("expected_discrepancy_types", [])) <= found_types:
                    discrepancy_correct += 1

            if full_pipeline and meta.get("dedup_role") == "near_duplicate":
                dedup_expected += 1
                codes = {flag.get("code") for flag in invoice.fraud_flags or []}
                if invoice.status is InvoiceStatus.HELD and meta.get("expected_flag") in codes:
                    dedup_caught += 1

            from app.services.cost_ledger import invoice_cost

            llm_cost += await invoice_cost(session, invoice.id)

    metrics: dict[str, Any] = scores.summary()
    if catch_rates:
        metrics["validation_catch_rate"] = round(sum(catch_rates) / len(catch_rates), 4)
    if matching_total:
        metrics["matching_accuracy"] = round(matching_correct / matching_total, 4)
        metrics["discrepancy_type_accuracy"] = round(discrepancy_correct / matching_total, 4)
    if dedup_expected:
        metrics["dedup_recall"] = round(dedup_caught / dedup_expected, 4)
        metrics["dedup_false_holds"] = false_dup_holds
    metrics["llm_cost_usd_total"] = str(llm_cost)

    return {
        "config": args.config,
        "mode": args.mode,
        "dataset": args.dataset,
        "model": args.model or "fake-llm" if args.mode == "fixture" else args.model,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "metrics": metrics,
    }


def _write_reports(report: dict[str, Any]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = REPORTS_DIR / f"{stamp}_{report['config']}_{report['dataset']}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    _refresh_latest()
    return base.with_suffix(".json")


def _refresh_latest() -> None:
    latest: dict[str, dict[str, Any]] = {}
    for path in sorted(REPORTS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        latest[data["config"]] = data

    lines = [
        "# APilot — Latest Eval Report",
        "",
        "One row per configuration (latest run of each). Fixture mode replays",
        "recorded model outputs through the real pipeline; live mode calls the API.",
        "",
        "| Configuration | Mode | Totals F1 | Line items F1 | Doc exact match | "
        "Seeded errors caught | Matching acc. | Dedup recall |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        data = latest.get(config)
        if not data:
            continue
        m = data["metrics"]

        def cell(value: Any) -> str:
            return "—" if value is None else str(value)

        lines.append(
            f"| {config} | {data['mode']} | {cell(m.get('totals_f1'))} | "
            f"{cell(m.get('line_items_f1'))} | {cell(m.get('doc_exact_match'))} | "
            f"{cell(m.get('validation_catch_rate'))} | "
            f"{cell(m.get('matching_accuracy'))} | {cell(m.get('dedup_recall'))} |"
        )
    lines += [
        "",
        f"_Refreshed {datetime.now(UTC).isoformat(timespec='seconds')} — see "
        "`evals/reports/` for full per-run JSON, including per-field F1._",
        "",
    ]
    Path("evals/latest_report.md").write_text("\n".join(lines))


def _check_against_baseline(report: dict[str, Any]) -> int:
    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    baseline_all = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}
    baseline = baseline_all.get(report["config"], {})
    delta = thresholds.get("regression_delta", 0.02)
    # Floors are per-config: rungs of the ladder without a validation layer
    # legitimately score 0 on catch-rate.
    floors = thresholds.get("floors", {}).get(report["config"], {})
    failures = []
    for metric in thresholds.get("gate_metrics", []):
        current = report["metrics"].get(metric)
        if current is None:
            continue
        floor = floors.get(metric)
        if floor is not None and current < floor:
            failures.append(f"{metric}={current} below floor {floor}")
        previous = baseline.get(metric)
        if previous is not None and current < previous - delta:
            failures.append(f"{metric}={current} regressed from baseline {previous}")
    if failures:
        print("EVAL GATE FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("eval gate passed")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    parser.add_argument("--config", choices=CONFIGS, default="schema_retry")
    parser.add_argument("--dataset", default="v1")
    parser.add_argument("--model", default=None)
    parser.add_argument("--check", action="store_true", help="gate against baseline")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    if args.mode == "fixture" and args.config == "paged":
        raise SystemExit("config 'paged' needs real multi-page documents — run with --mode live")

    report = asyncio.run(_run(args))
    path = _write_reports(report)
    print(json.dumps(report["metrics"], indent=2))
    print(f"report: {path}")

    if args.update_baseline:
        baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}
        baseline[report["config"]] = report["metrics"]
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
        print(f"baseline updated for '{report['config']}'")

    if args.check:
        sys.exit(_check_against_baseline(report))


if __name__ == "__main__":
    main()
