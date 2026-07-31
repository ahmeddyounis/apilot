from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Vendor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "vendors"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    tax_id: Mapped[str | None] = mapped_column(Text)
    default_currency: Mapped[str | None] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
