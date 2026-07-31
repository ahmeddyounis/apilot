"""MCP server: the invoice pipeline as tools any MCP client can call.

Each tool opens its own session and calls the same services the HTTP API uses —
there is no second implementation of the pipeline.
"""

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from mcp.server.mcpserver import MCPServer
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.llm.embeddings import build_embedding_provider
from app.llm.factory import build_llm_client
from app.models import Invoice, InvoiceFile, InvoiceStatus, MatchingResult
from app.services.cost_ledger import invoice_cost
from app.services.dedup import run_screening_stage
from app.services.extraction import run_extraction
from app.services.matching import run_matching_stage
from app.services.state_machine import transition
from app.services.storage import sniff_mime
from app.services.validation import run_validation_stage

mcp = MCPServer(
    "apilot",
    instructions=(
        "Invoice intelligence: extract structured data from invoice documents, "
        "screen for duplicates and fraud, and 3-way match against purchase orders."
    ),
)


@mcp.tool()
async def extract_invoice(file_path: str) -> dict[str, Any]:
    """Extract structured data from a local invoice PDF or image, then run the
    deterministic validation rules over it.

    Returns the extracted fields, validation findings, per-invoice LLM cost, and
    the invoice id for use with the other tools. Extraction failures are reported
    honestly — the system never guesses at a value it could not read.
    """
    settings = get_settings()
    path = Path(file_path).expanduser()  # noqa: ASYNC240 — path arithmetic, no I/O
    if not await asyncio.to_thread(path.is_file):
        return {"error": f"No such file: {path}"}
    data = await asyncio.to_thread(path.read_bytes)
    mime = sniff_mime(data[:16])
    if mime is None or mime not in settings.allowed_mime_types:
        return {"error": f"Unsupported file type for {path.name}"}

    async with get_sessionmaker()() as session:
        file_row = InvoiceFile(
            sha256=f"{uuid4().hex}{uuid4().hex}"[:64],
            mime=mime,
            size_bytes=len(data),
            storage_path=str(path.resolve()),
            original_filename=path.name,
        )
        session.add(file_row)
        await session.flush()
        invoice = Invoice(status=InvoiceStatus.RECEIVED, file_id=file_row.id)
        session.add(invoice)
        await session.flush()
        await session.refresh(invoice, attribute_names=["file"])
        await transition(session, invoice, InvoiceStatus.EXTRACTING, actor="mcp")
        await session.commit()

        llm = build_llm_client(settings, model=settings.extraction_model)
        await run_extraction(session, invoice, llm=llm, settings=settings)

        if invoice.status is not InvoiceStatus.EXTRACTED:
            return {
                "invoice_id": str(invoice.id),
                "status": invoice.status.value,
                "error_detail": invoice.error_detail,
            }

        await run_validation_stage(session, invoice, settings=settings)
        await session.refresh(invoice, attribute_names=["line_items"])
        cost = await invoice_cost(session, invoice.id)
        return {
            "invoice_id": str(invoice.id),
            "status": invoice.status.value,
            "vendor_name": invoice.vendor_name,
            "invoice_number": invoice.invoice_number,
            "invoice_date": str(invoice.invoice_date),
            "due_date": str(invoice.due_date) if invoice.due_date else None,
            "currency": invoice.currency,
            "po_number": invoice.po_number,
            "subtotal": str(invoice.subtotal),
            "tax_amount": str(invoice.tax_amount),
            "total": str(invoice.total),
            "line_items": [
                {
                    "line_no": line.line_no,
                    "description": line.description,
                    "quantity": str(line.quantity),
                    "unit_price": str(line.unit_price),
                    "amount": str(line.amount),
                }
                for line in invoice.line_items
            ],
            "field_confidence": invoice.field_confidence,
            "validation": invoice.validation_results,
            "llm_cost_usd": str(cost),
        }


@mcp.tool()
async def check_duplicates(invoice_id: str) -> dict[str, Any]:
    """Screen an extracted invoice for duplicates and fraud signals.

    Runs exact-hash and near-duplicate (embedding) detection plus the heuristic
    risk flags, and reports each flag with a written explanation. Flags never
    auto-reject — a flagged invoice is held for human review.
    """
    settings = get_settings()
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, UUID(invoice_id))
        if invoice is None:
            return {"error": f"Invoice {invoice_id} not found"}
        if invoice.status is not InvoiceStatus.EXTRACTED:
            return {
                "error": f"Invoice is in status '{invoice.status.value}'; "
                "screening runs on extracted invoices"
            }
        embedder = build_embedding_provider(settings)
        proceeded = await run_screening_stage(
            session, invoice, settings=settings, embedder=embedder
        )
        return {
            "invoice_id": invoice_id,
            "status": invoice.status.value,
            "held": not proceeded,
            "hold_reason": invoice.hold_reason,
            "risk_score": str(invoice.risk_score) if invoice.risk_score else None,
            "flags": invoice.fraud_flags or [],
        }


@mcp.tool()
async def match_invoice(invoice_id: str) -> dict[str, Any]:
    """Run 3-way matching (invoice ↔ purchase order ↔ receiving records).

    Returns the structured verdict — per-check results, quantified discrepancies,
    a human-readable summary — plus the replayable agent trace. The verdict is
    assembled by deterministic code; the model only investigates ambiguity.
    """
    settings = get_settings()
    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, UUID(invoice_id))
        if invoice is None:
            return {"error": f"Invoice {invoice_id} not found"}
        if invoice.status is InvoiceStatus.EXTRACTED:
            await transition(session, invoice, InvoiceStatus.MATCHING, actor="mcp")
            await session.commit()
        if invoice.status is not InvoiceStatus.MATCHING:
            return {
                "error": f"Invoice is in status '{invoice.status.value}'; "
                "matching runs on extracted invoices"
            }
        llm = build_llm_client(settings, model=settings.matching_model)
        await run_matching_stage(session, invoice, llm=llm, settings=settings)

        result = await session.scalar(
            select(MatchingResult)
            .where(MatchingResult.invoice_id == invoice.id)
            .order_by(MatchingResult.created_at.desc())
            .limit(1)
        )
        return {
            "invoice_id": invoice_id,
            "status": invoice.status.value,
            "verdict": result.verdict if result else None,
            "trace": result.trace if result else [],
        }


def run() -> None:
    mcp.run(transport="stdio")
