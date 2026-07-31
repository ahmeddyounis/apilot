import httpx
import pytest

from app.core.config import get_settings
from app.main import create_app
from tests.integration.conftest import mint_key

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.4 auth flow test"


async def test_unknown_key_is_401(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/invoices",
        headers={"X-API-Key": "apk_test_not-a-real-key", "Idempotency-Key": "k1"},
        files={"file": ("a.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_revoked_key_is_401(client: httpx.AsyncClient) -> None:
    raw_key, row = await mint_key(["ingest"])
    from datetime import UTC, datetime

    from app.core.db import get_sessionmaker
    from app.models import ApiKey

    async with get_sessionmaker()() as session:
        api_key = await session.get(ApiKey, row.id)
        assert api_key is not None
        api_key.revoked_at = datetime.now(UTC)
        await session.commit()

    response = await client.post(
        "/api/v1/invoices",
        headers={"X-API-Key": raw_key, "Idempotency-Key": "k1"},
        files={"file": ("a.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 401


async def test_missing_scope_is_403(client: httpx.AsyncClient) -> None:
    raw_key, _ = await mint_key(["read"])  # no ingest
    response = await client.post(
        "/api/v1/invoices",
        headers={"X-API-Key": raw_key, "Idempotency-Key": "k1"},
        files={"file": ("a.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 403
    assert "ingest" in response.json()["error"]["message"]


async def test_rate_limit_returns_429_with_retry_after() -> None:
    raw_key, _ = await mint_key(["read"])
    settings = get_settings().model_copy(update={"rate_limit_default_per_min": 2})
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-API-Key": raw_key}
        first = await client.get(
            "/api/v1/invoices/00000000-0000-0000-0000-000000000000", headers=headers
        )
        assert first.status_code == 404  # authenticated, rate-limited dep passed
        await client.get("/api/v1/invoices/00000000-0000-0000-0000-000000000000", headers=headers)
        third = await client.get(
            "/api/v1/invoices/00000000-0000-0000-0000-000000000000", headers=headers
        )
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "rate_limited"
    assert int(third.headers["Retry-After"]) >= 1
