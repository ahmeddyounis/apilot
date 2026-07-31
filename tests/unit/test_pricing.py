from decimal import Decimal

import pytest

from app.llm.pricing import compute_cost
from app.llm.types import LLMUsage

pytestmark = pytest.mark.unit


def test_basic_cost_opus() -> None:
    usage = LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert compute_cost(usage, "claude-opus-5") == Decimal("30.000000")


def test_cache_token_rates() -> None:
    # 1M cache-write at 1.25x$5 + 1M cache-read at 0.10x$5
    usage = LLMUsage(cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert compute_cost(usage, "claude-opus-5") == Decimal("6.750000")


def test_realistic_invoice_call() -> None:
    usage = LLMUsage(input_tokens=2100, output_tokens=640, cache_read_input_tokens=900)
    # 2100*5/1M + 640*25/1M + 900*0.5/1M = 0.0105 + 0.016 + 0.00045
    assert compute_cost(usage, "claude-opus-5") == Decimal("0.026950")


def test_unknown_model_is_not_billed_as_free() -> None:
    """This used to assert 0, which is a lie shaped like an answer: the per-invoice
    cost ledger reported $0.00 for real spend on any model missing from the
    table."""
    usage = LLMUsage(input_tokens=1000, output_tokens=1000)
    assert compute_cost(usage, "mystery-model") > Decimal(0)


def test_fake_model_is_free() -> None:
    usage = LLMUsage(input_tokens=99999, output_tokens=99999)
    assert compute_cost(usage, "fake-llm") == Decimal("0.000000")


def test_output_config_matches_what_the_sdk_would_send() -> None:
    """We build output_config ourselves to keep client-side validation on our side
    of the call. The request the API sees must be identical to messages.parse()'s,
    or we've traded a crash for a silently weaker schema constraint."""
    import anthropic
    from pydantic import TypeAdapter

    from app.llm.anthropic_client import _json_schema_format
    from app.schemas.extraction_wire import InvoiceExtractionWire

    ours = _json_schema_format(InvoiceExtractionWire)
    # Exactly the two lines messages.parse() runs to build its own output_format.
    sdk_schema = anthropic.transform_schema(TypeAdapter(InvoiceExtractionWire).json_schema())

    assert ours["type"] == "json_schema"
    assert ours["schema"] == sdk_schema


def test_unknown_model_is_estimated_not_reported_as_free() -> None:
    """Returning 0 made the cost ledger — the thing this module exists to produce
    — report $0.00 for real spend whenever the configured model wasn't in the
    table, and analytics and the eval harness both inherit that number."""
    from app.llm.pricing import PRICING, compute_cost
    from app.llm.types import LLMUsage

    usage = LLMUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    cost = compute_cost(usage, "claude-some-model-we-have-not-priced")

    assert cost > 0
    priciest = max(PRICING.values(), key=lambda p: p.output_per_mtok)
    assert cost == priciest.input_per_mtok + priciest.output_per_mtok
