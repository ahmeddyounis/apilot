"""MCP server: the invoice pipeline as tools any MCP client can call.

Each tool opens its own session and calls the same services the HTTP API uses —
there is no second implementation of the pipeline.
"""

import asyncio
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from sqlalchemy import select

from app.api.errors import UnsupportedMediaError
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.security import hash_api_key
from app.llm.embeddings import build_embedding_provider
from app.llm.factory import build_llm_client
from app.models import ApiKey, Invoice, InvoiceFile, InvoiceStatus, MatchingResult
from app.services.cost_ledger import invoice_cost
from app.services.dedup import run_screening_stage
from app.services.extraction import run_extraction
from app.services.matching import run_matching_stage
from app.services.state_machine import transition
from app.services.storage import store_bytes
from app.services.validation import run_validation_stage

mcp = MCPServer(
    "apilot",
    instructions=(
        "Invoice intelligence: extract structured data from invoice documents, "
        "screen for duplicates and fraud, and 3-way match against purchase orders."
    ),
)


async def _authorize(scope: str) -> str | None:
    """Check APILOT_MCP_API_KEY against the same key table the HTTP API uses.

    stdio transport carries no headers, so the credential comes from the process
    environment the MCP client launches this server with. Without it these tools
    were an unauthenticated side door into the same pipeline the HTTP API guards
    with scopes and rate limits — reachable by anything that could spawn the
    process, and reading whatever local file it was pointed at.

    Returns an error string to hand back to the caller, or None when authorised.
    """
    raw_key = os.environ.get("APILOT_MCP_API_KEY", "").strip()
    if not raw_key:
        return (
            "APILOT_MCP_API_KEY is not set. Mint one with: "
            "python -m app.cli keys create --name mcp --scopes ingest,read"
        )
    async with get_sessionmaker()() as session:
        api_key = await session.scalar(
            select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key))
        )
        if api_key is None or api_key.revoked_at is not None:
            return "APILOT_MCP_API_KEY is not a valid, active API key"
        if scope not in api_key.scopes:
            return f"API key lacks the '{scope}' scope"
    return None


@mcp.tool()
async def extract_invoice(file_path: str) -> dict[str, Any]:
    """Extract structured data from a local invoice PDF or image, then run the
    deterministic validation rules over it.

    Returns the extracted fields, validation findings, per-invoice LLM cost, and
    the invoice id for use with the other tools. Extraction failures are reported
    honestly — the system never guesses at a value it could not read.
    """
    if (denied := await _authorize("ingest")) is not None:
        return {"error": denied}

    settings = get_settings()
    path = Path(file_path).expanduser()  # noqa: ASYNC240 — path arithmetic, no I/O
    if not await asyncio.to_thread(path.is_file):
        return {"error": f"No such file: {path}"}
    if await asyncio.to_thread(lambda: path.stat().st_size) > settings.max_upload_bytes:
        return {"error": f"File exceeds the {settings.max_upload_bytes}-byte limit"}
    data = await asyncio.to_thread(path.read_bytes)

    # Copy into content-addressed storage under the real hash, exactly as an HTTP
    # upload would. Recording a random sha256 and pointing storage_path at the
    # caller's file meant DUP-EXACT-FILE could never fire for anything ingested
    # here — the duplicate screen compares hashes — and that later pipeline stages
    # broke if the caller moved or deleted the file out from under them.
    try:
        stored = await store_bytes(
            data,
            storage_dir=settings.storage_dir,
            allowed_mime=settings.allowed_mime_types,
            original_filename=path.name,
        )
    except UnsupportedMediaError as exc:
        return {"error": str(exc)}

    async with get_sessionmaker()() as session:
        file_row = await session.scalar(
            select(InvoiceFile).where(InvoiceFile.sha256 == stored.sha256)
        )
        if file_row is None:
            file_row = InvoiceFile(
                sha256=stored.sha256,
                mime=stored.mime,
                size_bytes=stored.size_bytes,
                storage_path=stored.storage_path,
                original_filename=stored.original_filename,
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
    if (denied := await _authorize("read")) is not None:
        return {"error": denied}

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
    if (denied := await _authorize("read")) is not None:
        return {"error": denied}

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
