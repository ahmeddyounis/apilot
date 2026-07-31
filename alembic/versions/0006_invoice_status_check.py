"""invoice status CHECK constraint

Migration 0001 declared the column with native_enum=False, intending VARCHAR +
CHECK. SQLAlchemy's Enum.create_constraint has defaulted to False since 1.4, so
the CHECK was never emitted and the column accepted any string — a value outside
InvoiceStatus stored fine and then raised LookupError on every ORM read, leaving
a row the API could not return and the worker could not process.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op
from app.models.invoice import InvoiceStatus

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "invoice_status"


def upgrade() -> None:
    values = ", ".join(f"'{status.value}'" for status in InvoiceStatus)
    op.execute(f"ALTER TABLE invoices ADD CONSTRAINT {_CONSTRAINT} CHECK (status IN ({values}))")


def downgrade() -> None:
    op.execute(f"ALTER TABLE invoices DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
