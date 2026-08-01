"""A corpus large enough that an unbounded query shows up in the plan.

Deliberately not a benchmark. Wall-clock assertions on a laptop that is also running
Docker are a flake generator, and they measure the machine rather than the query. What
these tests assert instead is *shape*: does the planner reach for an index, and does
the number of statements stay flat as the corpus grows. Both are properties of the
code, so they fail for a reason someone can act on.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Select, event, insert, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_engine, get_sessionmaker
from app.models import Invoice, InvoiceFile, InvoiceStatus

CORPUS_SIZE = 400


@pytest.fixture
async def corpus() -> int:
    """Insert CORPUS_SIZE invoices spread across the statuses the queries filter on."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        file_id = uuid4()
        await session.execute(
            insert(InvoiceFile).values(
                id=file_id,
                sha256=uuid4().hex + uuid4().hex,
                mime="application/pdf",
                size_bytes=1024,
                storage_path="/dev/null",
                original_filename="corpus.pdf",
            )
        )
        statuses = [
            InvoiceStatus.EXCEPTION,
            InvoiceStatus.HELD,
            InvoiceStatus.APPROVED,
            InvoiceStatus.EXPORTED,
        ]
        await session.execute(
            insert(Invoice),
            [
                {
                    "id": uuid4(),
                    "status": statuses[index % len(statuses)],
                    "file_id": file_id,
                    "vendor_name": f"Vendor {index % 40}",
                    "invoice_number": f"INV-{index}",
                    "normalized_invoice_number": f"INV{index}",
                    "invoice_date": now.date(),
                    "currency": "USD",
                    "total": Decimal(index % 900 + 1),
                    "risk_score": Decimal("0.250"),
                    "received_at": now - timedelta(minutes=index),
                    "decided_at": now - timedelta(minutes=index),
                    "extraction_attempts": 1,
                    "pipeline_requeues": 0,
                }
                for index in range(CORPUS_SIZE)
            ],
        )
        await session.commit()

    # The planner will not use a new index until the table has been analyzed.
    async with get_engine().begin() as conn:
        await conn.execute(text("ANALYZE invoices"))
    return CORPUS_SIZE


async def explain(session: AsyncSession, statement: Select[Any]) -> str:
    """The plan Postgres would use, as text — literal_binds so EXPLAIN sees real
    values, since the planner's choice depends on them."""
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    rows = (await session.execute(text(f"EXPLAIN {compiled}"))).all()
    return "\n".join(row[0] for row in rows)


class StatementCounter:
    """Counts SQL round trips, which is what "bounded work" actually means here."""

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> "StatementCounter":
        event.listen(get_engine().sync_engine, "before_cursor_execute", self._bump)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(get_engine().sync_engine, "before_cursor_execute", self._bump)

    def _bump(self, *_: object, **__: object) -> None:
        self.count += 1
