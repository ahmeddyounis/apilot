from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Date, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Receipt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A receiving record: what actually arrived against a purchase order."""

    __tablename__ = "receipts"

    receipt_number: Mapped[str] = mapped_column(Text, nullable=False)
    po_id: Mapped[UUID] = mapped_column(ForeignKey("purchase_orders.id"), nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)

    line_items: Mapped[list["ReceiptLineItem"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )


class ReceiptLineItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "receipt_line_items"

    receipt_id: Mapped[UUID] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False
    )
    po_line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    receipt: Mapped[Receipt] = relationship(back_populates="line_items")
