import httpx
import pytest

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.unit


def make_client() -> httpx.AsyncClient:
    app = create_app(Settings(_env_file=None))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_health_is_ok_and_carries_request_id() -> None:
    async with make_client() as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers.get("X-Request-ID")


async def test_inbound_request_id_is_echoed() -> None:
    async with make_client() as client:
        response = await client.get("/health", headers={"X-Request-ID": "req-abc-123"})
    assert response.headers["X-Request-ID"] == "req-abc-123"


async def test_unknown_route_returns_error_envelope() -> None:
    async with make_client() as client:
        response = await client.get("/definitely-not-a-route")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"]
    assert error["details"] == []
