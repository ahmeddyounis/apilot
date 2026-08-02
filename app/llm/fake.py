"""Deterministic fixture-driven LLM for tests, CI, and offline dev.

Resolution order for the fixture key:

1. An explicit scenario set via the ``scenario(...)`` context manager (contextvars
   propagate through ``await``, so tests can wrap a whole pipeline run).
2. The first 16 hex chars of sha256 over all document bytes — stable per content.

A fixture value is either a single record or a **list** of records consumed one per
call (scripting retry sequences). Records:

    {"wire": {...}}                          → parsed output (validated against the schema)
    {"stop_reason": "refusal"}               → refusal outcome
    {"parse_error": "..."}                    → unparseable-output outcome
    {"text": "..."}                           → generate_text output
    optional: {"usage": {"input_tokens": ...}}

Missing fixtures fail loudly — determinism beats silent invention.
"""

import contextlib
import hashlib
import json
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.llm.types import (
    DocumentInput,
    FeedbackContext,
    FixtureMissingError,
    LLMUsage,
    ParseResult,
    TextResult,
    ToolTurn,
    describe_schema_mismatch,
)

FAKE_MODEL_ID = "fake-llm"

_scenario: ContextVar[str | None] = ContextVar("fake_llm_scenario", default=None)


@contextlib.contextmanager
def scenario(name: str) -> Iterator[None]:
    token = _scenario.set(name)
    try:
        yield
    finally:
        _scenario.reset(token)


def _docs_key(docs: list[DocumentInput]) -> str:
    hasher = hashlib.sha256()
    for doc in docs:
        hasher.update(doc.data)
    return hasher.hexdigest()[:16]


class FakeLLM:
    model = FAKE_MODEL_ID

    def __init__(self, fixtures_dirs: list[Path] | None = None) -> None:
        self._fixtures: dict[str, Any] = {}
        self._call_counts: dict[str, int] = {}
        for directory in fixtures_dirs or []:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                self._fixtures.update(json.loads(path.read_text()))

    def add_fixtures(self, fixtures: dict[str, Any]) -> None:
        self._fixtures.update(fixtures)

    def _next_record(self, key: str) -> dict[str, Any]:
        if key not in self._fixtures:
            raise FixtureMissingError(
                f"FakeLLM has no fixture for key '{key}'. Add it to a fixtures JSON "
                f"(tests/fixtures/llm/ or seed/fixtures/llm/) or wrap the call in "
                f"fake.scenario('<key>')."
            )
        value = self._fixtures[key]
        if isinstance(value, list):
            index = min(self._call_counts.get(key, 0), len(value) - 1)
            self._call_counts[key] = index + 1
            record = value[index]
        else:
            record = value
        if not isinstance(record, dict):
            raise FixtureMissingError(f"Fixture '{key}' must be an object or list of objects")
        return record

    @staticmethod
    def _usage(record: dict[str, Any]) -> LLMUsage:
        return LLMUsage.model_validate(
            record.get("usage", {"input_tokens": 1000, "output_tokens": 500})
        )

    async def parse_document[T: BaseModel](
        self,
        *,
        docs: list[DocumentInput],
        system: str,
        user_text: str,
        output_schema: type[T],
        feedback: FeedbackContext | None = None,
        max_tokens: int = 16384,
    ) -> ParseResult[T]:
        key = _scenario.get() or _docs_key(docs)
        record = self._next_record(key)
        usage = self._usage(record)

        if record.get("stop_reason") == "refusal":
            return ParseResult(
                parsed=None,
                raw_text=None,
                usage=usage,
                model_id=FAKE_MODEL_ID,
                stop_reason="refusal",
            )
        if "parse_error" in record:
            return ParseResult(
                parsed=None,
                raw_text=record.get("raw_text", "{not-json"),
                usage=usage,
                model_id=FAKE_MODEL_ID,
                stop_reason="end_turn",
                parse_error=str(record["parse_error"]),
            )

        wire_payload = record.get("wire")
        if wire_payload is None:
            raise FixtureMissingError(f"Fixture '{key}' has neither 'wire' nor a failure form")
        try:
            parsed = output_schema.model_validate(wire_payload)
        except ValidationError as exc:
            # A fixture may deliberately violate the schema to exercise the
            # unparseable-output path.
            return ParseResult(
                parsed=None,
                raw_text=json.dumps(wire_payload),
                usage=usage,
                model_id=FAKE_MODEL_ID,
                stop_reason="end_turn",
                parse_error=describe_schema_mismatch(exc),
            )
        return ParseResult(
            parsed=parsed,
            raw_text=json.dumps(wire_payload),
            usage=usage,
            model_id=FAKE_MODEL_ID,
            stop_reason="end_turn",
        )

    async def generate_text(
        self,
        *,
        docs: list[DocumentInput],
        system: str,
        user_text: str,
        max_tokens: int = 16384,
    ) -> TextResult:
        key = _scenario.get() or _docs_key(docs)
        record = self._next_record(key)
        return TextResult(
            text=str(record.get("text", record.get("wire", ""))),
            usage=self._usage(record),
            model_id=FAKE_MODEL_ID,
            stop_reason="end_turn",
        )

    async def tool_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 8192,
    ) -> ToolTurn:
        """Fixture forms: {"tool_use": {"name", "input"}} or {"final_text": "..."}.

        Keyed as ``<scenario>:tool_turn`` so one pipeline scenario can carry both an
        extraction wire fixture and an investigation script.
        """
        base = _scenario.get()
        if base is None:
            raise FixtureMissingError(
                "FakeLLM.tool_turn requires an explicit fake.scenario(...) key"
            )
        key = f"{base}:tool_turn"
        record = self._next_record(key)
        usage = self._usage(record)
        if "tool_use" in record:
            call = dict(record["tool_use"])
            call_id = call.pop("id", f"fake-tool-{self._call_counts.get(key, 0)}")
            content: list[dict[str, Any]] = [
                {"type": "tool_use", "id": call_id, "name": call["name"], "input": call["input"]}
            ]
            return ToolTurn(
                content=content, stop_reason="tool_use", usage=usage, model_id=FAKE_MODEL_ID
            )
        final_text = str(record.get("final_text", ""))
        return ToolTurn(
            content=[{"type": "text", "text": final_text}],
            stop_reason="end_turn",
            usage=usage,
            model_id=FAKE_MODEL_ID,
        )
