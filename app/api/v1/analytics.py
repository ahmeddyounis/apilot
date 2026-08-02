from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.core.config import get_settings
from app.core.db import get_session
from app.services.analytics import summary

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
async def analytics_summary(
    since: datetime | None = Query(
        default=None,
        description=(
            "Start of the reporting window. Defaults to the last 90 days — the "
            "resolved window is returned in the response."
        ),
    ),
    _auth: AuthContext = Depends(require_scopes("read")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await summary(
        session,
        since=since,
        default_window_days=get_settings().analytics_default_window_days,
    )
