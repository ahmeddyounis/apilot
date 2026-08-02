"""indexes and a generated sort key so the hot queries have visible bounds

Five of these are ordinary missing indexes. The interesting one is
`review_priority`: the exceptions queue sorted by a CASE over two coalesced mutable
columns, which no index can serve, so Postgres computed it for every exception and
held invoice on every page of every request. Materialising it as a stored generated
column makes the sort an index scan — and keeps it correct by construction, since
Postgres recomputes it on write and nothing in Python can forget to.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors app/api/v1/exceptions.py's old in-query expression exactly: risk weighted by
# the money at stake, falling back to risk alone when there is no total to weight by.
# 0.5 for an unscreened invoice — mid-priority, neither ignored nor jumped to the top.
_PRIORITY_SQL = (
    "CASE WHEN COALESCE(total, 0) > 0 "
    "THEN COALESCE(risk_score, 0.5) * COALESCE(total, 0) "
    "ELSE COALESCE(risk_score, 0.5) END"
)


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE invoices ADD COLUMN review_priority NUMERIC(18, 3) "
        f"GENERATED ALWAYS AS ({_PRIORITY_SQL}) STORED"
    )
    # Partial: exception and held are the only statuses this queue shows, and they are
    # also the two that accumulate — so the index stays a fraction of the table.
    op.execute(
        "CREATE INDEX ix_invoices_review_queue ON invoices "
        "(review_priority DESC, received_at ASC) "
        "WHERE status IN ('exception', 'held')"
    )

    # The duplicate check filters on this alone; the only index covering it led with
    # vendor_id, which the query does not constrain, so it was unusable and the scan
    # was sequential — on the ingest hot path, once per invoice.
    op.create_index(
        "ix_invoices_normalized_invoice_number",
        "invoices",
        ["normalized_invoice_number"],
        postgresql_where=sa.text("normalized_invoice_number IS NOT NULL"),
    )
    op.create_index("ix_invoices_decided_at", "invoices", ["decided_at"])  # exports sort
    op.create_index("ix_invoices_updated_at", "invoices", ["updated_at"])  # stale-job cron
    op.create_index("ix_invoice_embeddings_model_name", "invoice_embeddings", ["model_name"])
    # llm_calls is the fastest-growing table — a row per attempt, retries included —
    # and analytics scanned it three times per request with neither column indexed.
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])
    op.create_index("ix_llm_calls_model", "llm_calls", ["model"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_model", table_name="llm_calls")
    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_index("ix_invoice_embeddings_model_name", table_name="invoice_embeddings")
    op.drop_index("ix_invoices_updated_at", table_name="invoices")
    op.drop_index("ix_invoices_decided_at", table_name="invoices")
    op.drop_index("ix_invoices_normalized_invoice_number", table_name="invoices")
    op.execute("DROP INDEX IF EXISTS ix_invoices_review_queue")
    op.drop_column("invoices", "review_priority")
