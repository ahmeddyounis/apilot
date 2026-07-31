from typing import Any

import pytest

from app.llm.fake import FakeLLM, scenario
from app.llm.types import DocumentInput, FixtureMissingError
from app.schemas.extraction_wire import InvoiceExtractionWire

pytestmark = pytest.mark.unit

DOC = DocumentInput(kind="pdf", data=b"%PDF-1.4 fake", media_type="application/pdf")

VALID_WIRE: dict[str, Any] = {
    "vendor_name": "V",
    "vendor_tax_id": None,
    "invoice_number": "I-1",
    "invoice_date": "2026-01-01",
    "due_date": None,
    "currency": "USD",
    "po_number": None,
    "line_items": [],
    "subtotal": "0",
    "tax_amount": "0",
    "total": "0",
    "payment_details_present": False,
    "payment_details_raw": None,
    "field_confidence": {},
    "source_page_refs": {},
}


async def parse(fake: FakeLLM) -> object:
    return await fake.parse_document(
        docs=[DOC], system="s", user_text="u", output_schema=InvoiceExtractionWire
    )


async def test_missing_fixture_fails_loudly() -> None:
    fake = FakeLLM()
    with pytest.raises(FixtureMissingError, match="no fixture"):
        await parse(fake)


async def test_scenario_key_and_valid_wire() -> None:
    fake = FakeLLM()
    fake.add_fixtures({"case-1": {"wire": VALID_WIRE}})
    with scenario("case-1"):
        result = await parse(fake)
    assert result.parsed is not None  # type: ignore[attr-defined]
    assert result.parsed.vendor_name == "V"  # type: ignore[attr-defined]


async def test_scripted_sequence_advances_then_sticks() -> None:
    fake = FakeLLM()
    fake.add_fixtures({"seq": [{"parse_error": "bad output"}, {"wire": VALID_WIRE}]})
    with scenario("seq"):
        first = await parse(fake)
        second = await parse(fake)
        third = await parse(fake)
    assert first.parse_error == "bad output"  # type: ignore[attr-defined]
    assert second.parsed is not None  # type: ignore[attr-defined]
    assert third.parsed is not None  # type: ignore[attr-defined]  # last record repeats


async def test_refusal_record() -> None:
    fake = FakeLLM()
    fake.add_fixtures({"nope": {"stop_reason": "refusal"}})
    with scenario("nope"):
        result = await parse(fake)
    assert result.stop_reason == "refusal"  # type: ignore[attr-defined]
    assert result.parsed is None  # type: ignore[attr-defined]


async def test_invalid_wire_fixture_becomes_parse_error() -> None:
    fake = FakeLLM()
    fake.add_fixtures({"bad": {"wire": {"vendor_name": "only-this"}}})
    with scenario("bad"):
        result = await parse(fake)
    assert result.parsed is None  # type: ignore[attr-defined]
    assert "schema" in (result.parse_error or "")  # type: ignore[attr-defined]


async def test_content_hash_key_without_scenario() -> None:
    fake = FakeLLM()
    key = DOC.sha256_hex[:16]
    fake.add_fixtures({key: {"wire": VALID_WIRE}})
    result = await parse(fake)
    assert result.parsed is not None  # type: ignore[attr-defined]
