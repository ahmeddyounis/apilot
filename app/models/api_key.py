from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # A credential that can approve payments should stop working on its own if
    # nobody remembers to retire it. NULL means non-expiring, which stays the
    # default: forcing an expiry on the demo key would break `make demo` a month
    # after anyone ran it.
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Set on the old key when `keys rotate` supersedes it, so the audit trail can
    # follow a credential across a rotation instead of seeing two unrelated keys.
    replaced_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL")
    )
