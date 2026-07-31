"""Provider-agnostic LLM types."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel


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


class LLMError(Exception):
    """Provider-layer failure that is not an API-level refusal."""


class FixtureMissingError(LLMError):
    """FakeLLM had no fixture for the requested key."""
