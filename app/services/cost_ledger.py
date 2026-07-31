"""Per-invoice LLM cost accounting. Every API attempt gets a row — retries too."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import LLM_COST_USD
from app.llm.pricing import compute_cost
from app.llm.types import LLMUsage
from app.models.llm_call import LlmCall

Purpose = Literal["extraction", "matching", "eval"]


def record_llm_call(
    session: AsyncSession,
    *,
    invoice_id: UUID | None,
    purpose: Purpose,
    usage: LLMUsage,
    model_id: str,
    latency_ms: int | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    success: bool = True,
) -> LlmCall:
    cost = compute_cost(usage, model_id)
    call = LlmCall(
        invoice_id=invoice_id,
        purpose=purpose,
        model=model_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        request_id=request_id,
        trace_id=trace_id,
        success=success,
    )
    session.add(call)
    LLM_COST_USD.labels(model=model_id, purpose=purpose).inc(float(cost))
    return call


async def invoice_cost(session: AsyncSession, invoice_id: UUID) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(LlmCall.invoice_id == invoice_id)
    )
    return Decimal(total or 0)
