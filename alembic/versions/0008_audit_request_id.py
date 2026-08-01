"""correlate audit rows with the request that caused them

The pipeline runs in a different process from the upload that starts it, and only
the invoice id crossed that boundary — so a worker failure could be traced to *which
key* uploaded the invoice, never to which request. Partial index because most
lookups are "show me everything this request did", and rows predating the column are
NULL forever: the append-only trigger forbids UPDATE, so there is no backfill.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN with no default is metadata-only, and DDL does not fire the
    # row-level append-only trigger.
    op.add_column("audit_log", sa.Column("request_id", sa.String(64)))
    op.create_index(
        "ix_audit_log_request_id",
        "audit_log",
        ["request_id"],
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_request_id", table_name="audit_log")
    op.drop_column("audit_log", "request_id")
