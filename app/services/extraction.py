"""Vision-LLM extraction with retry-with-feedback and honest terminal states.

Failure taxonomy (the load-bearing design decision):

* **Refusal** → terminal ``extraction_failed`` immediately; retrying a refusal is
  wasted spend.
* **Schema/coercion failures** (unparseable output, bad decimal strings, bad dates)
  → retry with feedback; terminal ``extraction_failed`` after max attempts. These are
  model-output defects — never guessed around.
* **Arithmetic inconsistencies** (line math, totals) → retry with feedback because
  they usually mean a misread digit; but if they persist, the extraction is
  **accepted as-is** — a faithfully-transcribed document whose printed numbers don't
  add up belongs in the validation engine's exception queue, not in
  ``extraction_failed``.
"""

import asyncio
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import structlog
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.normalize import normalize_invoice_number, normalize_vendor_name
from app.llm.client import LLMClient
from app.llm.types import DocumentInput, FeedbackContext, FixtureMissingError, ParseResult
from app.models import Invoice, InvoiceLineItem, InvoiceStatus, Vendor
from app.schemas.extraction import InvoiceExtraction, arithmetic_findings, wire_to_domain
from app.schemas.extraction_wire import (
    FieldConfidenceWire,
    InvoiceExtractionWire,
    LineItemWire,
    PageExtractionWire,
)
from app.services import audit
from app.services.cost_ledger import record_llm_call
from app.services.state_machine import transition

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts"
EXTRACTION_SYSTEM = (_PROMPTS_DIR / "extraction_system.md").read_text()
PAGE_EXTRACTION_SYSTEM = (_PROMPTS_DIR / "page_extraction_system.md").read_text()

USER_TEXT = "Extract the invoice data from the attached document."

_HEADER_FIELDS = (
    "vendor_name",
    "vendor_tax_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "po_number",
)
_TOTAL_FIELDS = ("subtotal", "tax_amount", "total")


@dataclass(frozen=True)
class ExtractionConfig:
    """Real production toggles — also the eval harness's configuration ladder."""

    structured_outputs: bool = True
    retry_with_feedback: bool = True
    max_retries: int = 2
    paged_threshold: int = 6
    force_paged: bool = False

    @property
    def max_attempts(self) -> int:
        return 1 + (self.max_retries if self.retry_with_feedback else 0)


# --- document preparation ---------------------------------------------------------


def _pdf_page_count(data: bytes) -> int:
    document = pdfium.PdfDocument(data)
    try:
        return len(document)
    finally:
        document.close()


def _render_pdf_pages(data: bytes, scale: float = 2.0) -> list[bytes]:
    document = pdfium.PdfDocument(data)
    try:
        pages: list[bytes] = []
        for index in range(len(document)):
            bitmap = document[index].render(scale=scale)
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            pages.append(buffer.getvalue())
        return pages
    finally:
        document.close()


def _tiff_to_png(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()


# --- naive mode parsing -----------------------------------------------------------


def parse_json_loose(text: str) -> tuple[InvoiceExtractionWire | None, str | None]:
    """Best-effort JSON recovery for the eval harness's 'naive single prompt' mode."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.startswith("json"):
            candidate = candidate[4:]
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None, "no JSON object found in output"
    try:
        payload = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    try:
        return InvoiceExtractionWire.model_validate(payload), None
    except Exception as exc:
        return None, f"JSON did not match the extraction schema: {exc}"


# --- page merge -------------------------------------------------------------------


def merge_pages(pages: list[PageExtractionWire]) -> dict[str, Any]:
    """Deterministically merge per-page extractions into a wire-shaped payload.

    Header fields: highest confidence wins, earliest page breaks ties. Totals: taken
    from the last page presenting a totals block (per-field last-non-null fallback).
    Line items: concatenated in page order, dropping a page-leading row identical to
    the previous page's last row (carried-forward repeats).
    """
    confidence: dict[str, float] = {}
    refs: dict[str, int] = {}
    merged: dict[str, Any] = {}

    for field in _HEADER_FIELDS:
        best: tuple[float, int, str] | None = None  # (confidence, page, value)
        for page in pages:
            value = getattr(page, field)
            if value is None:
                continue
            page_conf = getattr(page.field_confidence, field)
            conf = page_conf if page_conf is not None else 0.5
            if best is None or conf > best[0]:
                best = (conf, page.page_number, value)
        if best is not None:
            merged[field] = best[2]
            confidence[field] = best[0]
            refs[field] = best[1]
        else:
            merged[field] = None

    totals_page = next((page for page in reversed(pages) if page.total is not None), None)
    for field in _TOTAL_FIELDS:
        source = totals_page
        if source is None or getattr(source, field) is None:
            source = next((p for p in reversed(pages) if getattr(p, field) is not None), None)
        if source is not None:
            merged[field] = getattr(source, field)
            refs[field] = source.page_number
            page_conf = getattr(source.field_confidence, field)
            if page_conf is not None:
                confidence[field] = page_conf
        else:
            merged[field] = None

    line_items: list[LineItemWire] = []
    line_confidences: list[float] = []
    first_line_page: int | None = None
    for page in pages:
        page_lines = list(page.line_items)
        if line_items and page_lines and page_lines[0].model_dump() == line_items[-1].model_dump():
            page_lines = page_lines[1:]  # carried-forward repeat of the previous row
        if page_lines and first_line_page is None:
            first_line_page = page.page_number
        if page.line_items and page.field_confidence.line_items is not None:
            line_confidences.append(page.field_confidence.line_items)
        line_items.extend(page_lines)
    merged["line_items"] = [line.model_dump() for line in line_items]
    if first_line_page is not None:
        refs["line_items"] = first_line_page
    if line_confidences:
        confidence["line_items"] = min(line_confidences)

    merged["payment_details_present"] = any(page.payment_details_present for page in pages)
    merged["payment_details_raw"] = next(
        (page.payment_details_raw for page in pages if page.payment_details_raw), None
    )
    if merged["payment_details_present"]:
        refs["payment_details_present"] = next(
            page.page_number for page in pages if page.payment_details_present
        )

    valid_conf_keys = set(FieldConfidenceWire.model_fields)
    merged["field_confidence"] = {
        key: value for key, value in confidence.items() if key in valid_conf_keys
    }
    merged["source_page_refs"] = dict(refs)
    return merged


# --- the extraction stage ---------------------------------------------------------


async def run_extraction(
    session: AsyncSession,
    invoice: Invoice,
    *,
    llm: LLMClient,
    settings: Settings,
    config: ExtractionConfig | None = None,
) -> None:
    """Run extraction for an invoice in status EXTRACTING; transitions to
    EXTRACTED or EXTRACTION_FAILED and commits."""
    config = config or ExtractionConfig()
    file = invoice.file
    data = await asyncio.to_thread(Path(file.storage_path).read_bytes)

    mime = file.mime
    if mime == "image/tiff":
        data = await asyncio.to_thread(_tiff_to_png, data)
        mime = "image/png"

    is_pdf = mime == "application/pdf"
    if is_pdf:
        try:
            page_count = await asyncio.to_thread(_pdf_page_count, data)
        except Exception as exc:
            # Magic bytes said PDF but the document doesn't parse: terminal, honest.
            await _fail(
                session,
                invoice,
                reason="unreadable_document",
                detail=f"Could not parse PDF: {exc}",
            )
            return
    else:
        page_count = 1
    paged = is_pdf and (config.force_paged or page_count > config.paged_threshold)

    try:
        if paged:
            await _run_paged(
                session, invoice, llm=llm, config=config, data=data, page_count=page_count
            )
        else:
            docs = [DocumentInput(kind="pdf" if is_pdf else "image", data=data, media_type=mime)]
            await _run_single(session, invoice, llm=llm, config=config, docs=docs)
    except FixtureMissingError as exc:
        # Fake mode without a fixture: terminal and honest, never invented.
        await _fail(session, invoice, reason="fixture_missing", detail=str(exc))


async def _run_single(
    session: AsyncSession,
    invoice: Invoice,
    *,
    llm: LLMClient,
    config: ExtractionConfig,
    docs: list[DocumentInput],
) -> None:
    feedback: FeedbackContext | None = None
    accepted: InvoiceExtraction | None = None
    accepted_wire: dict[str, Any] | None = None
    accepted_findings: list[str] = []
    last_errors: list[str] = []
    model_id = ""

    for attempt in range(1, config.max_attempts + 1):
        wire, result, errors = await _attempt(
            session,
            invoice,
            llm=llm,
            config=config,
            docs=docs,
            feedback=feedback,
            attempt=attempt,
            system=EXTRACTION_SYSTEM,
            schema=InvoiceExtractionWire,
        )
        model_id = result.model_id
        if result.stop_reason == "refusal":
            await _fail(
                session,
                invoice,
                reason="refusal",
                detail="The model declined to process this document.",
            )
            return
        if wire is None or errors:
            last_errors = errors or ["model produced no parseable output"]
            feedback = FeedbackContext(
                previous_raw_output=result.raw_text or "", errors=last_errors
            )
            continue

        domain, coercion_errors = wire_to_domain(wire)
        if domain is None:
            last_errors = coercion_errors
            feedback = FeedbackContext(
                previous_raw_output=result.raw_text or "", errors=coercion_errors
            )
            continue

        findings = arithmetic_findings(domain)
        accepted = domain
        accepted_wire = wire.model_dump()
        accepted_findings = findings
        if findings and attempt < config.max_attempts and config.retry_with_feedback:
            feedback = FeedbackContext(previous_raw_output=result.raw_text or "", errors=findings)
            continue
        break

    if accepted is None:
        await _fail(
            session,
            invoice,
            reason="schema_invalid_after_retries",
            detail="; ".join(last_errors)[:2000] or "no valid extraction produced",
        )
        return

    await _persist(
        session,
        invoice,
        extraction=accepted,
        wire_payload=accepted_wire or {},
        model_id=model_id,
        mode="single",
        arithmetic=accepted_findings,
    )


async def _run_paged(
    session: AsyncSession,
    invoice: Invoice,
    *,
    llm: LLMClient,
    config: ExtractionConfig,
    data: bytes,
    page_count: int,
) -> None:
    rendered = await asyncio.to_thread(_render_pdf_pages, data)
    pages: list[PageExtractionWire] = []
    model_id = ""

    for number, png in enumerate(rendered, start=1):
        docs = [
            DocumentInput(kind="image", data=png, media_type="image/png", label=f"page {number}")
        ]
        feedback: FeedbackContext | None = None
        page_wire: PageExtractionWire | None = None
        for attempt in range(1, config.max_attempts + 1):
            wire, result, errors = await _attempt(
                session,
                invoice,
                llm=llm,
                config=config,
                docs=docs,
                feedback=feedback,
                attempt=attempt,
                system=PAGE_EXTRACTION_SYSTEM,
                schema=PageExtractionWire,
                user_text=(
                    f"Extract the data visible on this page. This is page {number} of {page_count}."
                ),
            )
            model_id = result.model_id
            if result.stop_reason == "refusal":
                await _fail(
                    session,
                    invoice,
                    reason="refusal",
                    detail=f"The model declined to process page {number}.",
                )
                return
            if wire is None or errors:
                feedback = FeedbackContext(
                    previous_raw_output=result.raw_text or "",
                    errors=errors or ["model produced no parseable output"],
                )
                continue
            page_wire = wire
            break
        if page_wire is None:
            await _fail(
                session,
                invoice,
                reason="schema_invalid_after_retries",
                detail=f"page {number} produced no valid extraction",
            )
            return
        pages.append(page_wire)

    merged_payload = merge_pages(pages)
    try:
        merged_wire = InvoiceExtractionWire.model_validate(merged_payload)
    except Exception as exc:
        await _fail(
            session,
            invoice,
            reason="schema_invalid_after_retries",
            detail=f"merged page extraction incomplete: {exc}",
        )
        return

    domain, coercion_errors = wire_to_domain(merged_wire)
    if domain is None:
        await _fail(
            session,
            invoice,
            reason="schema_invalid_after_retries",
            detail="; ".join(coercion_errors)[:2000],
        )
        return

    await _persist(
        session,
        invoice,
        extraction=domain,
        wire_payload=merged_wire.model_dump(),
        model_id=model_id,
        mode="paged",
        arithmetic=arithmetic_findings(domain),
    )


async def _attempt(
    session: AsyncSession,
    invoice: Invoice,
    *,
    llm: LLMClient,
    config: ExtractionConfig,
    docs: list[DocumentInput],
    feedback: FeedbackContext | None,
    attempt: int,
    system: str,
    schema: type,
    user_text: str = USER_TEXT,
) -> tuple[Any, ParseResult[Any], list[str]]:
    """One LLM attempt: call, ledger row, audit row, commit. Returns (wire|None,
    result, schema-level errors)."""
    started = time.monotonic()
    errors: list[str] = []
    wire: Any = None
    result: ParseResult[Any]

    if config.structured_outputs:
        result = await llm.parse_document(
            docs=docs,
            system=system,
            user_text=user_text,
            output_schema=schema,
            feedback=feedback,
        )
        if result.parse_error:
            errors.append(result.parse_error)
        else:
            wire = result.parsed
    else:
        from app.llm.anthropic_client import render_feedback

        text = user_text if feedback is None else f"{user_text}\n\n{render_feedback(feedback)}"
        naive_system = (
            "You extract invoice data. Respond ONLY with a JSON object with keys: "
            "vendor_name, vendor_tax_id, invoice_number, invoice_date, due_date, "
            "currency, po_number, line_items (description, quantity, unit_price, "
            "amount), subtotal, tax_amount, total, payment_details_present, "
            "payment_details_raw, field_confidence, source_page_refs. Amounts as "
            "plain strings, dates ISO."
        )
        text_result = await llm.generate_text(docs=docs, system=naive_system, user_text=text)
        result = ParseResult(
            parsed=None,
            raw_text=text_result.text,
            usage=text_result.usage,
            model_id=text_result.model_id,
            stop_reason=text_result.stop_reason,
            request_id=text_result.request_id,
        )
        if result.stop_reason != "refusal":
            parsed_wire, parse_error = parse_json_loose(text_result.text)
            if parse_error:
                errors.append(parse_error)
            wire = parsed_wire

    latency_ms = int((time.monotonic() - started) * 1000)
    invoice.extraction_attempts += 1
    record_llm_call(
        session,
        invoice_id=invoice.id,
        purpose="extraction",
        usage=result.usage,
        model_id=result.model_id,
        latency_ms=latency_ms,
        request_id=result.request_id,
        success=result.stop_reason != "refusal" and not errors,
    )
    audit.record(
        session,
        entity_type="invoice",
        entity_id=invoice.id,
        invoice_id=invoice.id,
        event_type="extraction_attempt",
        actor="worker",
        payload={
            "attempt": attempt,
            "model": result.model_id,
            "stop_reason": result.stop_reason,
            "errors": errors[:5],
            "had_feedback": feedback is not None,
        },
    )
    await session.commit()
    return wire, result, errors


async def _persist(
    session: AsyncSession,
    invoice: Invoice,
    *,
    extraction: InvoiceExtraction,
    wire_payload: dict[str, Any],
    model_id: str,
    mode: str,
    arithmetic: list[str],
) -> None:
    invoice.vendor_name = extraction.vendor_name
    invoice.invoice_number = extraction.invoice_number
    invoice.normalized_invoice_number = normalize_invoice_number(extraction.invoice_number)
    invoice.invoice_date = extraction.invoice_date
    invoice.due_date = extraction.due_date
    invoice.currency = extraction.currency
    invoice.po_number = extraction.po_number
    invoice.subtotal = extraction.subtotal
    invoice.tax_amount = extraction.tax_amount
    invoice.total = extraction.total
    invoice.payment_details_present = extraction.payment_details_present
    invoice.extraction_model = model_id
    invoice.extraction_mode = mode
    invoice.field_confidence = dict(extraction.field_confidence)
    invoice.source_page_refs = dict(extraction.source_page_refs)

    # The immutable raw record — the model's own output, minus the wire-only
    # remittance text which must never be persisted.
    raw = dict(wire_payload)
    raw.pop("payment_details_raw", None)
    invoice.extraction = raw

    vendor = await session.scalar(
        select(Vendor).where(
            Vendor.normalized_name == normalize_vendor_name(extraction.vendor_name)
        )
    )
    invoice.vendor_id = vendor.id if vendor is not None else None

    for index, line in enumerate(extraction.line_items, start=1):
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id,
                line_no=index,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
            )
        )

    await transition(
        session,
        invoice,
        InvoiceStatus.EXTRACTED,
        actor="worker",
        payload={
            "mode": mode,
            "model": model_id,
            "attempts": invoice.extraction_attempts,
            "arithmetic_findings": arithmetic[:10],
        },
    )
    await session.commit()
    logger.info(
        "extraction_succeeded",
        mode=mode,
        attempts=invoice.extraction_attempts,
        arithmetic_findings=len(arithmetic),
    )


async def _fail(session: AsyncSession, invoice: Invoice, *, reason: str, detail: str) -> None:
    invoice.error_detail = detail
    await transition(
        session,
        invoice,
        InvoiceStatus.EXTRACTION_FAILED,
        actor="worker",
        reason=reason,
    )
    await session.commit()
    logger.info("extraction_failed", reason=reason)
