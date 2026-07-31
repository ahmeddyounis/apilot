"""Model pricing (USD per million tokens) and cost computation.

Cache economics: writes bill at 1.25x the input rate, reads at 0.10x.
"""

from dataclasses import dataclass
from decimal import Decimal

import structlog

from app.llm.types import LLMUsage

logger = structlog.get_logger(__name__)

_MTOK = Decimal(1_000_000)
_CACHE_WRITE_MULTIPLIER = Decimal("1.25")
_CACHE_READ_MULTIPLIER = Decimal("0.10")


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: Decimal
    output_per_mtok: Decimal

    @property
    def cache_write_per_mtok(self) -> Decimal:
        return self.input_per_mtok * _CACHE_WRITE_MULTIPLIER

    @property
    def cache_read_per_mtok(self) -> Decimal:
        return self.input_per_mtok * _CACHE_READ_MULTIPLIER


PRICING: dict[str, ModelPricing] = {
    "claude-opus-5": ModelPricing(Decimal(5), Decimal(25)),
    "claude-sonnet-5": ModelPricing(Decimal(3), Decimal(15)),
    "claude-haiku-4-5": ModelPricing(Decimal(1), Decimal(5)),
    "fake-llm": ModelPricing(Decimal(0), Decimal(0)),
}

_SIX_DP = Decimal("0.000001")


def compute_cost(usage: LLMUsage, model_id: str) -> Decimal:
    pricing = PRICING.get(model_id)
    if pricing is None:
        # Zero is a lie that looks like an answer: point APILOT_EXTRACTION_MODEL at
        # any model not in the table and the per-invoice cost ledger — the thing
        # this module exists to produce — reports $0.00 for real spend, with the
        # analytics endpoint and the eval harness's headline cost figure both
        # inheriting it. Estimate at the most expensive known rate and say so, so
        # the number is wrong in the direction that gets noticed.
        fallback = max(PRICING.values(), key=lambda p: p.output_per_mtok)
        logger.error(
            "unknown_model_pricing_estimated",
            model=model_id,
            hint="add it to PRICING; cost is an upper-bound estimate until then",
        )
        pricing = fallback
    cost = (
        Decimal(usage.input_tokens) * pricing.input_per_mtok
        + Decimal(usage.output_tokens) * pricing.output_per_mtok
        + Decimal(usage.cache_creation_input_tokens) * pricing.cache_write_per_mtok
        + Decimal(usage.cache_read_input_tokens) * pricing.cache_read_per_mtok
    ) / _MTOK
    return cost.quantize(_SIX_DP)
