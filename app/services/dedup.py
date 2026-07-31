"""Duplicate & fraud screening — layered, cheapest first.

1. Exact: same file bytes; same normalized (vendor, invoice number) key.
2. Near-duplicate: embedding cosine over canonical text, cross-checked with fuzzy
   vendor / amount-within-1% / date-within-7-days scoring.
3. Heuristic risk flags F-100..F-140.

Flags never auto-reject: the worst outcome is ``held`` with a written explanation.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.normalize import normalize_vendor_name
from app.llm.embeddings import EmbeddingProvider
from app.models import (
    Invoice,
    InvoiceEmbedding,
    InvoiceStatus,
    PurchaseOrder,
    VendorPaymentFingerprint,
)
from app.services import audit
from app.services.state_machine import transition

logger = structlog.get_logger(__name__)

_EXCLUDED_STATUSES = (InvoiceStatus.EXTRACTION_FAILED, InvoiceStatus.REJECTED)

_SEVERITY_WEIGHTS = {"high": Decimal("0.40"), "medium": Decimal("0.20"), "low": Decimal("0.05")}
_DUP_WEIGHT = Decimal("0.50")


@dataclass(frozen=True)
class Flag:
    code: str
    severity: str  # high | medium | low
    explanation: str
    evidence: dict[str, Any]

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "explanation": self.explanation,
            "evidence": self.evidence,
        }


def canonical_text(invoice: Invoice) -> str:
    lines = "|".join(
        f"{line.description} {line.quantity} {line.unit_price}" for line in invoice.line_items
    )
    return (
        f"{normalize_vendor_name(invoice.vendor_name or '')}|"
        f"{invoice.normalized_invoice_number or ''}|{invoice.invoice_date}|"
        f"{invoice.currency}|{invoice.total}|{lines}"
    )


def combined_dup_score(
    *,
    cosine: float,
    vendor_a: str,
    vendor_b: str,
    total_a: Decimal,
    total_b: Decimal,
    date_a: date | None,
    date_b: date | None,
) -> float:
    vendor_similarity = fuzz.token_sort_ratio(vendor_a, vendor_b) / 100.0
    amount_close = 0.0
    if total_a and total_b:
        larger = max(abs(total_a), abs(total_b))
        if larger > 0 and abs(total_a - total_b) / larger <= Decimal("0.01"):
            amount_close = 1.0
    date_close = 0.0
    if date_a is not None and date_b is not None and abs((date_a - date_b).days) <= 7:
        date_close = 1.0
    return 0.45 * cosine + 0.25 * vendor_similarity + 0.20 * amount_close + 0.10 * date_close


def risk_score(flags: list[Flag]) -> Decimal:
    score = Decimal(0)
    for flag in flags:
        if flag.code.startswith("DUP-"):
            score += _DUP_WEIGHT
        else:
            score += _SEVERITY_WEIGHTS.get(flag.severity, Decimal("0.05"))
    return min(Decimal(1), score).quantize(Decimal("0.001"))


def should_hold(flags: list[Flag]) -> bool:
    if any(flag.code.startswith("DUP-") for flag in flags):
        return True
    if any(flag.severity == "high" for flag in flags):
        return True
    return sum(1 for flag in flags if flag.severity == "medium") >= 2


# --- the screening stage -------------------------------------------------------------


async def run_screening_stage(
    session: AsyncSession,
    invoice: Invoice,
    *,
    settings: Settings,
    embedder: EmbeddingProvider,
) -> bool:
    """Screen an EXTRACTED invoice. Returns True to proceed to matching; on dup or
    high-risk signals routes to HELD and returns False. Commits either way."""
    await session.refresh(invoice, attribute_names=["line_items"])
    flags: list[Flag] = []

    flags.extend(await _exact_duplicate_flags(session, invoice))
    flags.extend(await _near_duplicate_flags(session, invoice, settings, embedder))
    flags.extend(await _heuristic_flags(session, invoice, settings))

    invoice.fraud_flags = [flag.to_jsonb() for flag in flags]
    invoice.risk_score = risk_score(flags)

    audit.record(
        session,
        entity_type="invoice",
        entity_id=invoice.id,
        invoice_id=invoice.id,
        event_type="screening_completed",
        actor="worker",
        payload={
            "flags": [flag.code for flag in flags],
            "risk_score": str(invoice.risk_score),
        },
    )

    if should_hold(flags):
        holding = next(
            (f for f in flags if f.code.startswith("DUP-") or f.severity == "high"), flags[0]
        )
        await transition(
            session,
            invoice,
            InvoiceStatus.HELD,
            actor="worker",
            reason=f"{holding.code}: {holding.explanation}",
            payload={"flags": [flag.code for flag in flags]},
        )
        await session.commit()
        return False

    await session.commit()
    return True


async def _exact_duplicate_flags(session: AsyncSession, invoice: Invoice) -> list[Flag]:
    flags: list[Flag] = []

    same_file = await session.scalar(
        select(Invoice)
        .where(
            Invoice.file_id == invoice.file_id,
            Invoice.id != invoice.id,
            Invoice.status.not_in(_EXCLUDED_STATUSES),
        )
        .limit(1)
    )
    if same_file is not None:
        flags.append(
            Flag(
                code="DUP-EXACT-FILE",
                severity="high",
                explanation=(
                    "Identical file bytes were already ingested as invoice "
                    f"{same_file.id} ({same_file.status.value})"
                ),
                evidence={"matched_invoice_id": str(same_file.id)},
            )
        )

    if invoice.normalized_invoice_number:
        conditions = [
            Invoice.normalized_invoice_number == invoice.normalized_invoice_number,
            Invoice.id != invoice.id,
            Invoice.status.not_in(_EXCLUDED_STATUSES),
        ]
        candidates = (await session.scalars(select(Invoice).where(*conditions))).all()
        this_vendor = normalize_vendor_name(invoice.vendor_name or "")
        for candidate in candidates:
            same_vendor = (
                invoice.vendor_id is not None and candidate.vendor_id == invoice.vendor_id
            ) or normalize_vendor_name(candidate.vendor_name or "") == this_vendor
            if same_vendor:
                flags.append(
                    Flag(
                        code="DUP-EXACT-KEY",
                        severity="high",
                        explanation=(
                            f"Vendor + invoice number match existing invoice {candidate.id} "
                            f"({candidate.status.value})"
                        ),
                        evidence={"matched_invoice_id": str(candidate.id)},
                    )
                )
                break
    return flags


async def _near_duplicate_flags(
    session: AsyncSession,
    invoice: Invoice,
    settings: Settings,
    embedder: EmbeddingProvider,
) -> list[Flag]:
    text = canonical_text(invoice)
    vector = embedder.embed([text])[0]

    # Upsert this invoice's embedding, then search the rest of the corpus.
    await session.execute(delete(InvoiceEmbedding).where(InvoiceEmbedding.invoice_id == invoice.id))
    session.add(
        InvoiceEmbedding(invoice_id=invoice.id, embedding=vector, model_name=embedder.model_name)
    )
    await session.flush()

    distance = InvoiceEmbedding.embedding.cosine_distance(vector)
    rows = (
        await session.execute(
            select(Invoice, distance.label("distance"))
            .join(Invoice, Invoice.id == InvoiceEmbedding.invoice_id)
            .where(
                InvoiceEmbedding.invoice_id != invoice.id,
                InvoiceEmbedding.model_name == embedder.model_name,
                Invoice.status.not_in(_EXCLUDED_STATUSES),
            )
            .order_by(distance.asc())
            .limit(10)
        )
    ).all()

    flags: list[Flag] = []
    for candidate, dist in rows:
        cosine = 1.0 - float(dist)
        if cosine < settings.near_dup_cosine_threshold:
            continue
        score = combined_dup_score(
            cosine=cosine,
            vendor_a=invoice.vendor_name or "",
            vendor_b=candidate.vendor_name or "",
            total_a=invoice.total or Decimal(0),
            total_b=candidate.total or Decimal(0),
            date_a=invoice.invoice_date,
            date_b=candidate.invoice_date,
        )
        if score >= settings.dup_suspect_threshold:
            flags.append(
                Flag(
                    code="DUP-SUSPECT",
                    severity="high",
                    explanation=(
                        f"Near-duplicate of invoice {candidate.id} "
                        f"({candidate.invoice_number}): combined score {score:.2f} "
                        f"(cosine {cosine:.2f}) — same content re-billed under a "
                        "different invoice number is the classic double-payment vector"
                    ),
                    evidence={
                        "matched_invoice_id": str(candidate.id),
                        "matched_invoice_number": candidate.invoice_number,
                        "cosine": round(cosine, 3),
                        "combined_score": round(score, 3),
                    },
                )
            )
            break
    return flags


async def trust_payment_fingerprint(session: AsyncSession, invoice: Invoice) -> str | None:
    """Record this invoice's remittance account as known-good for the vendor.

    Screening deliberately refuses to learn an account whose arrival triggered
    F-110, so something has to close the loop for the legitimate case — a vendor
    really does change banks. That something is a human approving the invoice:
    the reviewer looked at the flag and accepted it, which is exactly the evidence
    the automated path lacks. Returns the fingerprint if it was newly trusted.
    """
    if invoice.vendor_id is None or not invoice.payment_fingerprint:
        return None
    already = await session.scalar(
        select(VendorPaymentFingerprint.id).where(
            VendorPaymentFingerprint.vendor_id == invoice.vendor_id,
            VendorPaymentFingerprint.fingerprint == invoice.payment_fingerprint,
        )
    )
    if already is not None:
        return None
    session.add(
        VendorPaymentFingerprint(
            vendor_id=invoice.vendor_id,
            fingerprint=invoice.payment_fingerprint,
            first_seen_invoice_id=invoice.id,
        )
    )
    return invoice.payment_fingerprint


async def _heuristic_flags(
    session: AsyncSession, invoice: Invoice, settings: Settings
) -> list[Flag]:
    flags: list[Flag] = []
    total = invoice.total or Decimal(0)

    prior_count = 0
    if invoice.vendor_id is not None:
        prior_count = len(
            (
                await session.scalars(
                    select(Invoice.id).where(
                        Invoice.vendor_id == invoice.vendor_id,
                        Invoice.id != invoice.id,
                        Invoice.status.not_in(_EXCLUDED_STATUSES),
                    )
                )
            ).all()
        )

    if invoice.vendor_id is None or prior_count == 0:
        flags.append(
            Flag(
                code="F-100",
                severity="low",
                explanation="First invoice ever seen from this vendor",
                evidence={"vendor_name": invoice.vendor_name},
            )
        )

    # Bank-detail signals need a known vendor and fingerprint history.
    if invoice.vendor_id is not None:
        known = (
            await session.scalars(
                select(VendorPaymentFingerprint).where(
                    VendorPaymentFingerprint.vendor_id == invoice.vendor_id
                )
            )
        ).all()
        known_prints = {row.fingerprint for row in known}
        if invoice.payment_fingerprint:
            changed = bool(known_prints) and invoice.payment_fingerprint not in known_prints
            if changed:
                flags.append(
                    Flag(
                        code="F-110",
                        severity="high",
                        explanation=(
                            "Remittance bank details differ from this vendor's known "
                            "payment details — the classic AP fraud vector"
                        ),
                        evidence={"known_fingerprints": len(known_prints)},
                    )
                )
            elif not known_prints and prior_count >= 3:
                flags.append(
                    Flag(
                        code="F-115",
                        severity="medium",
                        explanation=(
                            "Bank details appeared for the first time on an established vendor"
                        ),
                        evidence={"prior_invoices": prior_count},
                    )
                )
            # Learn the account only when arrival isn't itself the alarm. Recording
            # it unconditionally meant F-110 disarmed itself: the attacker's details
            # became "known" the moment they were flagged, so the very next invoice
            # on that account sailed through clean. A changed account earns trust
            # only when a human approves the invoice carrying it (see
            # `trust_payment_fingerprint`).
            if not changed and invoice.payment_fingerprint not in known_prints:
                session.add(
                    VendorPaymentFingerprint(
                        vendor_id=invoice.vendor_id,
                        fingerprint=invoice.payment_fingerprint,
                        first_seen_invoice_id=invoice.id,
                    )
                )

    is_round_hundred = (
        total >= settings.round_number_min_total
        and total == total.to_integral_value()
        and int(total) % 100 == 0
    )
    if is_round_hundred:
        flags.append(
            Flag(
                code="F-120",
                severity="low",
                explanation=f"Suspiciously round total ({total} {invoice.currency})",
                evidence={"total": str(total)},
            )
        )

    threshold = settings.auto_approve_max_total
    if Decimal("0.95") * threshold <= total < threshold:
        flags.append(
            Flag(
                code="F-130",
                severity="medium",
                explanation=(
                    f"Total {total} sits just under the auto-approve threshold "
                    f"({threshold}) — a known structuring pattern"
                ),
                evidence={"total": str(total), "threshold": str(threshold)},
            )
        )

    if invoice.po_number and invoice.invoice_date is not None:
        po_order_date = await session.scalar(
            select(PurchaseOrder.order_date).where(
                PurchaseOrder.po_number == invoice.po_number.strip()
            )
        )
        if po_order_date is not None and invoice.invoice_date < po_order_date:
            flags.append(
                Flag(
                    code="F-140",
                    severity="medium",
                    explanation=(
                        f"Invoice dated {invoice.invoice_date} before its purchase "
                        f"order ({po_order_date})"
                    ),
                    evidence={
                        "invoice_date": str(invoice.invoice_date),
                        "po_order_date": str(po_order_date),
                    },
                )
            )
    return flags
