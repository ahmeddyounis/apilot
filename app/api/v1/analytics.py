from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.core.db import get_session
from app.services.analytics import summary

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
async def analytics_summary(
    since: datetime | None = Query(default=None),
    _auth: AuthContext = Depends(require_scopes("read")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await summary(session, since=since)
