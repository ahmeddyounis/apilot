"""Integration fixtures: real Postgres (pgvector) + Redis via testcontainers.

Containers are session-scoped and migrations run once (testing the real Alembic
path, not ``create_all``). Engines/clients are loop-bound, so they are disposed
after every test; tables are truncated before every test for isolation.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from alembic import command
from app.core.config import get_settings
from app.core.db import dispose_engine, get_engine, get_sessionmaker
from app.core.redis import close_arq_pool, close_redis
from app.core.security import generate_api_key
from app.main import create_app
from app.models import ApiKey, Base


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        "pgvector/pgvector:pg17", username="apilot", password="apilot", dbname="apilot"
    ) as container:
        yield container


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer("redis:8.2.3-alpine") as container:
        yield container


@pytest.fixture(scope="session", autouse=True)
def _configure_environment(
    pg_container: PostgresContainer,
    redis_container: RedisContainer,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    db_url = (
        f"postgresql+asyncpg://apilot:apilot@{pg_container.get_container_host_ip()}"
        f":{pg_container.get_exposed_port(5432)}/apilot"
    )
    redis_url = (
        f"redis://{redis_container.get_container_host_ip()}"
        f":{redis_container.get_exposed_port(6379)}/0"
    )
    os.environ["APILOT_DATABASE_URL"] = db_url
    os.environ["APILOT_REDIS_URL"] = redis_url
    os.environ["APILOT_STORAGE_DIR"] = str(tmp_path_factory.mktemp("storage"))
    get_settings.cache_clear()
    # Unit tests may have created an engine against the default URL; drop it.
    asyncio.run(dispose_engine())

    alembic_config = Config("alembic.ini")
    command.upgrade(alembic_config, "head")

    yield

    get_settings.cache_clear()


# asyncpg forbids multiple commands per prepared statement — keep these separate.
_ALL_TABLES = ", ".join(f'"{table}"' for table in Base.metadata.tables)
_CLEANUP_STATEMENTS = (
    # replica role bypasses the append-only audit triggers — a superuser-only
    # escape hatch that exists precisely for test cleanup.
    "SET session_replication_role = 'replica'",
    f"TRUNCATE TABLE {_ALL_TABLES} RESTART IDENTITY CASCADE",
    "SET session_replication_role = 'origin'",
)


@pytest.fixture(autouse=True)
async def _clean_database() -> AsyncIterator[None]:
    """Truncate all tables before each test; dispose loop-bound singletons after."""
    engine = get_engine()
    async with engine.begin() as conn:
        for statement in _CLEANUP_STATEMENTS:
            await conn.execute(text(statement))
    yield
    await dispose_engine()
    await close_arq_pool()
    await close_redis()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(get_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def mint_key(scopes: list[str], name: str = "test-key") -> tuple[str, ApiKey]:
    """Insert an API key row and return (raw_key, row)."""
    generated = generate_api_key("test")
    async with get_sessionmaker()() as session:
        row = ApiKey(
            name=name,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=scopes,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return generated.full_key, row


@pytest.fixture
async def full_access_key() -> str:
    raw_key, _ = await mint_key(["ingest", "read", "review", "export"])
    return raw_key
