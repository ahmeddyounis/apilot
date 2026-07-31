from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class ReviewAction(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "review_actions"

    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    api_key_id: Mapped[UUID] = mapped_column(ForeignKey("api_keys.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # approve|reject|correct
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_review_actions_invoice_id", "invoice_id"),)


class FieldCorrection(UUIDPrimaryKeyMixin, Base):
    """Reviewer corrections — the ground truth that feeds the eval dataset."""

    __tablename__ = "field_corrections"

    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    review_action_id: Mapped[UUID] = mapped_column(ForeignKey("review_actions.id"), nullable=False)
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_field_corrections_invoice_id", "invoice_id"),)
