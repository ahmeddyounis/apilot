"""Provider-agnostic LLM types."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ValidationError


@dataclass(frozen=True)
class DocumentInput:
    kind: Literal["pdf", "image"]
    data: bytes
    media_type: str  # application/pdf | image/png | image/jpeg | image/webp
    label: str | None = None  # e.g. "page 3"

    @property
    def sha256_hex(self) -> str:
        import hashlib

        return hashlib.sha256(self.data).hexdigest()


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(frozen=True)
class FeedbackContext:
    """Carried into a retry attempt: the prior raw output plus what was wrong."""

    previous_raw_output: str
    errors: list[str] = field(default_factory=list)


@dataclass
class ParseResult[T: BaseModel]:
    parsed: T | None
    raw_text: str | None
    usage: LLMUsage
    model_id: str
    stop_reason: str | None  # "refusal" is a first-class, terminal outcome
    parse_error: str | None = None
    request_id: str | None = None


@dataclass
class TextResult:
    text: str
    usage: LLMUsage
    model_id: str
    stop_reason: str | None
    request_id: str | None = None


@dataclass
class ToolTurn:
    """One assistant turn in a bounded tool-use loop (raw content blocks)."""

    content: list[dict]
    stop_reason: str | None
    usage: LLMUsage
    model_id: str
    request_id: str | None = None

    @property
    def tool_uses(self) -> list[dict]:
        return [block for block in self.content if block.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "".join(
            block.get("text", "") for block in self.content if block.get("type") == "text"
        )


def describe_schema_mismatch(exc: ValidationError, *, limit: int = 5) -> str:
    """Render a schema failure as locations and reasons — never the offending value.

    Pydantic's ``errors()`` entries carry an ``input`` key holding what failed to
    validate, and for a root-level mismatch that is the *entire* model payload. This
    string is stored on ``invoices.error_detail`` and served to any key with `read`,
    so including ``input`` published the extraction verbatim — including
    ``payment_details_raw``, the vendor's raw bank details, which the persistence
    path takes deliberate care to drop (``app/services/extraction.py`` pops it before
    writing the immutable extraction JSONB).

    ``loc`` and ``msg`` are what the retry prompt needs anyway: the model already has
    its own output, what it lacks is which fields were wrong and why.
    ``app/schemas/extraction.py`` builds its errors the same way.
    """
    parts = [
        f"{'.'.join(str(piece) for piece in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()[:limit]
    ]
    return "output did not match schema: " + "; ".join(parts)


class LLMError(Exception):
    """Provider-layer failure that is not an API-level refusal."""


class FixtureMissingError(LLMError):
    """FakeLLM had no fixture for the requested key."""
