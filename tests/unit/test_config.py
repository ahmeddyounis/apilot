from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings

pytestmark = pytest.mark.unit


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_defaults_target_local_compose_ports() -> None:
    settings = make_settings()
    assert "5433" in settings.database_url
    assert "6380" in settings.redis_url
    assert settings.auto_approve_max_total == Decimal("2500.00")


def test_llm_mode_resolves_fake_without_key() -> None:
    assert make_settings().resolved_llm_mode == "fake"
    assert make_settings(anthropic_api_key=SecretStr("")).resolved_llm_mode == "fake"


def test_llm_mode_resolves_live_with_key() -> None:
    settings = make_settings(anthropic_api_key=SecretStr("sk-ant-test"))
    assert settings.resolved_llm_mode == "live"


def test_llm_mode_fake_wins_even_with_key() -> None:
    settings = make_settings(llm_mode="fake", anthropic_api_key=SecretStr("sk-ant-test"))
    assert settings.resolved_llm_mode == "fake"


def test_llm_mode_live_is_explicit() -> None:
    # Explicit live is honored even without a key; the client factory is the
    # place that fails loudly in that case.
    assert make_settings(llm_mode="live").resolved_llm_mode == "live"
