from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.core.db import get_session
from app.schemas.po import ReceiptCreatedOut, ReceiptCreateIn
from app.services.purchase_orders import create_receipt

router = APIRouter(prefix="/receipts", tags=["matching-data"])


@router.post("", status_code=201, response_model=ReceiptCreatedOut)
async def create_receiving_record(
    payload: ReceiptCreateIn,
    auth: AuthContext = Depends(require_scopes("ingest")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> ReceiptCreatedOut:
    receipt = await create_receipt(session, payload, auth=auth)
    return ReceiptCreatedOut(
        id=receipt.id, receipt_number=receipt.receipt_number, po_id=receipt.po_id
    )
