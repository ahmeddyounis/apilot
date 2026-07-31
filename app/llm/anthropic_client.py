"""Anthropic implementation of the LLM client.

Key behaviors:

* PDFs go to the API natively as base64 ``document`` blocks; images as ``image``
  blocks — no client-side OCR.
* Structured extraction sends the wire schema as ``output_config`` and validates
  the response here, so a schema mismatch is data (``parse_error``) that feeds
  retry-with-feedback rather than an exception out of the SDK's own parser.
* The static system prompt is a cached block (``cache_control: ephemeral``); all
  per-invoice content lives in the user message so the cache prefix stays byte-stable.
* ``temperature``/``top_p``/``thinking`` are never sent (rejected or defaulted on
  current models); ``max_tokens`` leaves headroom for adaptive thinking.
* ``stop_reason == "refusal"`` is returned as data, not raised — callers treat it as
  a terminal extraction outcome and never retry it.
"""

import base64
import time
from typing import Any

import structlog
from anthropic import AsyncAnthropic, transform_schema
from anthropic.types import JSONOutputFormatParam, OutputConfigParam
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.config import Settings
from app.llm.types import (
    DocumentInput,
    FeedbackContext,
    LLMUsage,
    ParseResult,
    TextResult,
    ToolTurn,
)

logger = structlog.get_logger(__name__)

_FEEDBACK_TEMPLATE = """PREVIOUS ATTEMPT FAILED VALIDATION.

Your previous output:
```json
{previous}
```

Validation errors:
{errors}

Produce a corrected, complete JSON object for the same document. Re-read the
document for every field named above. If the document genuinely shows a value that
triggers an arithmetic inconsistency, transcribe it unchanged — do not invent
numbers to make the math work. Do not apologize or explain."""


def render_feedback(feedback: FeedbackContext) -> str:
    numbered = "\n".join(f"{i}. {err}" for i, err in enumerate(feedback.errors, start=1))
    return _FEEDBACK_TEMPLATE.format(previous=feedback.previous_raw_output, errors=numbered)


def _json_schema_format(output_schema: type[BaseModel]) -> JSONOutputFormatParam:
    """The same output_config the SDK's messages.parse() builds internally.

    transform_schema strips the JSON-schema constructs structured outputs doesn't
    accept, so this is byte-for-byte what parse() would have sent — we just keep
    the client-side validation on our side of the call.
    """
    schema = TypeAdapter(output_schema).json_schema()
    return JSONOutputFormatParam(schema=transform_schema(schema), type="json_schema")


def _content_blocks(docs: list[DocumentInput], text: str) -> list[Any]:
    blocks: list[Any] = []
    for doc in docs:
        data = base64.standard_b64encode(doc.data).decode()
        if doc.kind == "pdf":
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": data,
                    },
                }
            )
        else:
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": doc.media_type, "data": data},
                }
            )
    blocks.append({"type": "text", "text": text})
    return blocks


def _usage_from(response: Any) -> LLMUsage:
    usage = response.usage
    return LLMUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


class AnthropicClient:
    def __init__(self, settings: Settings, *, model: str) -> None:
        if settings.anthropic_api_key is None:
            raise RuntimeError("ANTHROPIC_API_KEY is required for live LLM mode")
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=120.0,
        )
        self.model = model

    def _system_blocks(self, system: str) -> list[dict[str, Any]]:
        # The static instruction block is the cacheable prefix — keep it byte-stable.
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

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
        text = user_text if feedback is None else f"{user_text}\n\n{render_feedback(feedback)}"
        started = time.monotonic()
        # output_config rather than output_format, deliberately. Both send the same
        # JSON-schema constraint to the API; the difference is client-side. Passing
        # output_format makes the SDK validate every text block on the way out and
        # raise pydantic's ValidationError straight through this call — which would
        # skip the parse_error branch below entirely, so retry-with-feedback (the
        # feature this client exists to support) was unreachable in live mode, and a
        # schema-mismatched response became an uncaught exception that left the
        # invoice stranded in `extracting`. Validating the text ourselves keeps the
        # usage counts, the raw output and the failure, which is what the retry
        # prompt needs to tell the model what was wrong.
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self._system_blocks(system),  # type: ignore[arg-type]
            messages=[{"role": "user", "content": _content_blocks(docs, text)}],
            output_config=OutputConfigParam(format=_json_schema_format(output_schema)),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        raw_text = next((block.text for block in response.content if block.type == "text"), None)
        usage = _usage_from(response)
        request_id = getattr(response, "_request_id", None)
        logger.info(
            "llm_parse_document",
            model=self.model,
            latency_ms=elapsed_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read=usage.cache_read_input_tokens,
            stop_reason=response.stop_reason,
        )

        if response.stop_reason == "refusal":
            return ParseResult(
                parsed=None,
                raw_text=raw_text,
                usage=usage,
                model_id=self.model,
                stop_reason="refusal",
                request_id=request_id,
            )

        parsed: T | None = None
        parse_error: str | None = None
        if raw_text is not None:
            try:
                parsed = output_schema.model_validate_json(raw_text)
            except ValidationError as exc:
                parse_error = f"output did not match schema: {exc.errors()[:5]}"
        else:
            parse_error = "model returned no text content"

        return ParseResult(
            parsed=parsed,
            raw_text=raw_text,
            usage=usage,
            model_id=self.model,
            stop_reason=str(response.stop_reason) if response.stop_reason else None,
            parse_error=parse_error,
            request_id=request_id,
        )

    async def generate_text(
        self,
        *,
        docs: list[DocumentInput],
        system: str,
        user_text: str,
        max_tokens: int = 16384,
    ) -> TextResult:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self._system_blocks(system),  # type: ignore[arg-type]
            messages=[{"role": "user", "content": _content_blocks(docs, user_text)}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return TextResult(
            text=text,
            usage=_usage_from(response),
            model_id=self.model,
            stop_reason=str(response.stop_reason) if response.stop_reason else None,
            request_id=getattr(response, "_request_id", None),
        )

    async def tool_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 8192,
    ) -> ToolTurn:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self._system_blocks(system),  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
        )
        content: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )
        return ToolTurn(
            content=content,
            stop_reason=str(response.stop_reason) if response.stop_reason else None,
            usage=_usage_from(response),
            model_id=self.model,
            request_id=getattr(response, "_request_id", None),
        )
