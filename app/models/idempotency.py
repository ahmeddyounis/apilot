from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IdempotencyKey(Base):
    """Scope = (api_key_id, endpoint, key). Replay returns the original response."""

    __tablename__ = "idempotency_keys"

    api_key_id: Mapped[UUID] = mapped_column(ForeignKey("api_keys.id"), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)

    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    invoice_id: Mapped[UUID | None] = mapped_column(ForeignKey("invoices.id"))
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_idempotency_keys_expires_at", "expires_at"),)
