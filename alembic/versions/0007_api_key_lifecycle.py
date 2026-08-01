"""api key expiry and rotation lineage

A credential that can approve payments should be able to stop working on its own,
and a rotation should be traceable as one credential succeeding another rather
than two unrelated keys appearing.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both nullable: existing keys keep working and are not retroactively expired.
    op.add_column("api_keys", sa.Column("expires_at", sa.TIMESTAMP(timezone=True)))
    op.add_column("api_keys", sa.Column("replaced_by_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_api_keys_replaced_by_id",
        "api_keys",
        "api_keys",
        ["replaced_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_api_keys_replaced_by_id", "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "replaced_by_id")
    op.drop_column("api_keys", "expires_at")
