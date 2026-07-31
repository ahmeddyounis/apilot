from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class MatchingResult(UUIDPrimaryKeyMixin, Base):
    """One row per matching run; GET returns the latest. Verdict + replayable trace."""

    __tablename__ = "matching_results"

    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    po_id: Mapped[UUID | None] = mapped_column(ForeignKey("purchase_orders.id"))
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_matching_results_invoice_id", "invoice_id"),)
