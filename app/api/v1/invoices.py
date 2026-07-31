"""Invoice upload and retrieval endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, rate_limited, require_scopes
from app.core.db import get_session
from app.schemas.invoice import InvoiceAcceptedOut, InvoiceDetailOut
from app.services.invoices import get_invoice_detail, ingest_upload

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post("", status_code=202, response_model=InvoiceAcceptedOut)
async def upload_invoice(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="Client-chosen key; retries with the same key and file are safe.",
    ),
    auth: AuthContext = Depends(require_scopes("ingest")),
    _rate_limit: None = Depends(rate_limited("ingest")),
    session: AsyncSession = Depends(get_session),
) -> InvoiceAcceptedOut:
    result = await ingest_upload(
        session,
        settings=request.app.state.settings,
        auth=auth,
        file=file,
        idempotency_key=idempotency_key,
    )
    if result.replayed:
        response.headers["Idempotency-Replay"] = "true"
    return InvoiceAcceptedOut(invoice_id=result.invoice_id, status=result.status)


@router.get("/{invoice_id}", response_model=InvoiceDetailOut)
async def get_invoice(
    invoice_id: UUID,
    _auth: AuthContext = Depends(require_scopes("read")),
    _rate_limit: None = Depends(rate_limited()),
    session: AsyncSession = Depends(get_session),
) -> InvoiceDetailOut:
    invoice = await get_invoice_detail(session, invoice_id)
    return InvoiceDetailOut.model_validate(invoice)
