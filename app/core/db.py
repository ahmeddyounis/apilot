"""Async SQLAlchemy engine/session plumbing (lazy singletons)."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # Connections were never rotated (pool_recycle defaults to -1), which a
            # PgBouncer or an NLB with an idle timeout eventually kills underneath us.
            pool_recycle=settings.db_pool_recycle_s,
            pool_timeout=settings.db_pool_timeout_s,
            connect_args={
                "server_settings": {
                    # One runaway query could otherwise hold a connection for as long
                    # as it liked; fifteen of those wedge the whole process.
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "application_name": "apilot",
                }
            },
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Services own commits; the dependency only closes."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
