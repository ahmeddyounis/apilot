import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import get_engine, get_sessionmaker
from app.models import AuditLog

pytestmark = pytest.mark.integration


async def test_alembic_version_is_current() -> None:
    engine = get_engine()
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert version == head


async def test_audit_log_is_append_only() -> None:
    async with get_sessionmaker()() as session:
        session.add(AuditLog(entity_type="invoice", event_type="test", actor="system", payload={}))
        await session.commit()

    engine = get_engine()
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text("UPDATE audit_log SET actor = 'tampered'"))
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text("DELETE FROM audit_log"))


async def test_vector_extension_installed() -> None:
    engine = get_engine()
    async with engine.connect() as conn:
        installed = (
            await conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname='vector'"))
        ).scalar()
    assert installed == 1
