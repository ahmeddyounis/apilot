"""The provider-agnostic LLM interface.

Everything upstream (extraction, agent, evals) programs against this protocol;
`AnthropicClient` and `FakeLLM` are the two implementations. The matching agent's
tool-turn method is added in its own milestone.
"""

from typing import Protocol

from pydantic import BaseModel

from app.llm.types import DocumentInput, FeedbackContext, ParseResult, TextResult


class LLMClient(Protocol):
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
        """Structured extraction from documents into `output_schema`."""
        ...

    async def generate_text(
        self,
        *,
        docs: list[DocumentInput],
        system: str,
        user_text: str,
        max_tokens: int = 16384,
    ) -> TextResult:
        """Free-text completion over documents (the eval harness's 'naive' mode)."""
        ...
