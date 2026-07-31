from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.core.db import get_session
from app.schemas.po import POCreatedOut, POCreateIn
from app.services.purchase_orders import create_purchase_order

router = APIRouter(prefix="/purchase-orders", tags=["matching-data"])


@router.post("", status_code=201, response_model=POCreatedOut)
async def create_po(
    payload: POCreateIn,
    auth: AuthContext = Depends(require_scopes("ingest")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> POCreatedOut:
    po = await create_purchase_order(session, payload, auth=auth)
    return POCreatedOut(
        id=po.id,
        po_number=po.po_number,
        vendor_id=po.vendor_id,
        total=po.total,
        currency=po.currency,
    )
