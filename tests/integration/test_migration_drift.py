"""The migrations must describe exactly the schema the models declare.

The test suite migrates with Alembic and the app queries through the models, so
drift between them is invisible until production: a column the models expect but
no migration creates, or an index only the database knows about. The second kind
is worse than it sounds — `alembic revision --autogenerate` writes a DROP for
anything it can't see in the metadata, so the next routine migration quietly
takes the HNSW index (and near-duplicate search's speed) with it.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection

from app.core.db import get_engine
from app.models import Base

pytestmark = pytest.mark.integration


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


async def test_models_and_migrations_agree() -> None:
    async with get_engine().connect() as conn:
        diffs = await conn.run_sync(_diff)

    assert diffs == [], (
        "Model definitions have drifted from the migrations. Each entry below is a "
        "change `alembic revision --autogenerate` would emit — write a real migration "
        "for it, or declare the object on the model if a migration already creates it "
        "with raw SQL:\n" + "\n".join(f"  - {d}" for d in diffs)
    )
