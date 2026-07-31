"""LLM client construction from settings."""

from pathlib import Path

from app.core.config import Settings
from app.llm.anthropic_client import AnthropicClient
from app.llm.client import LLMClient
from app.llm.fake import FakeLLM

_DEFAULT_FIXTURE_DIRS = [Path("tests/fixtures/llm"), Path("seed/fixtures/llm")]


def build_llm_client(settings: Settings, *, model: str | None = None) -> LLMClient:
    if settings.resolved_llm_mode == "live":
        return AnthropicClient(settings, model=model or settings.extraction_model)
    return FakeLLM(fixtures_dirs=_DEFAULT_FIXTURE_DIRS)
