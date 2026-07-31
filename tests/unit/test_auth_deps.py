from uuid import uuid4

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

from app.api.deps import AuthContext, authenticate, require_scopes
from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.unit


def build_app_with_probe() -> FastAPI:
    app = create_app(Settings(_env_file=None, rate_limit_enabled=False))
    router = APIRouter()

    @router.get("/probe", dependencies=[Depends(require_scopes("review"))])
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    return app


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_missing_api_key_is_401() -> None:
    app = build_app_with_probe()
    async with client_for(app) as client:
        response = await client.get("/probe")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_insufficient_scope_is_403() -> None:
    app = build_app_with_probe()
    app.dependency_overrides[authenticate] = lambda: AuthContext(
        key_id=uuid4(), name="test", scopes=frozenset({"read"})
    )
    async with client_for(app) as client:
        response = await client.get("/probe", headers={"X-API-Key": "apk_test_x"})
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "forbidden"
    assert "review" in error["message"]


async def test_sufficient_scope_passes() -> None:
    app = build_app_with_probe()
    app.dependency_overrides[authenticate] = lambda: AuthContext(
        key_id=uuid4(), name="test", scopes=frozenset({"review", "read"})
    )
    async with client_for(app) as client:
        response = await client.get("/probe", headers={"X-API-Key": "apk_test_x"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
