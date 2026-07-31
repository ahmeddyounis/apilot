from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class VendorPaymentFingerprint(Base, UUIDPrimaryKeyMixin):
    """HMAC fingerprints of a vendor's remittance details.

    Only the keyed hash is ever stored — raw bank data never touches the database.
    A new fingerprint on an established vendor is the classic AP-fraud signal
    (F-110 bank_details_changed).
    """

    __tablename__ = "vendor_payment_fingerprints"

    vendor_id: Mapped[UUID] = mapped_column(ForeignKey("vendors.id"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_invoice_id: Mapped[UUID | None] = mapped_column(ForeignKey("invoices.id"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("vendor_id", "fingerprint"),)
