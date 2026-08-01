"""The cost ledger: one row per LLM API attempt."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class LlmCall(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "llm_calls"

    invoice_id: Mapped[UUID | None] = mapped_column(ForeignKey("invoices.id"))
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_llm_calls_invoice_id", "invoice_id"),
        # This is the fastest-growing table in the system — one row per LLM
        # attempt, retries included — and analytics scans it three times per
        # request, filtering on created_at and grouping by model.
        Index("ix_llm_calls_created_at", "created_at"),
        Index("ix_llm_calls_model", "model"),
    )
