"""The provider-agnostic LLM interface.

Everything upstream (extraction, agent, evals) programs against this protocol;
`AnthropicClient` and `FakeLLM` are the two implementations. The matching agent's
tool-turn method is added in its own milestone.
"""

from typing import Any, Protocol

from pydantic import BaseModel

from app.llm.types import DocumentInput, FeedbackContext, ParseResult, TextResult, ToolTurn


class LLMClient(Protocol):
    # The model actually in use. Needed when a call fails before it returns anything:
    # the ledger row and the failure counter still have to say which model was asked,
    # and reaching for the configured model id instead would report `claude-opus-5`
    # for calls that FakeLLM served.
    model: str

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

    async def tool_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 8192,
    ) -> ToolTurn:
        """One assistant turn with tools available (the matching agent's loop)."""
        ...
