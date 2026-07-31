"""pipeline requeue counter

Separates "LLM calls spent on extraction" from "times the cron re-queued this
invoice". They were the same column, so an invoice that used its extraction
retries reached the matching stage with its requeue budget already exhausted.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default so the NOT NULL holds for rows that already exist; the ORM
    # default covers new ones.
    op.add_column(
        "invoices",
        sa.Column("pipeline_requeues", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("invoices", "pipeline_requeues")
