from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import DEV_FINGERPRINT_SECRET, Settings

pytestmark = pytest.mark.unit


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_defaults_target_local_compose_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    # The integration conftest exports container URLs; strip them so this test
    # sees the true class defaults regardless of suite ordering.
    for var in ("APILOT_DATABASE_URL", "APILOT_REDIS_URL", "APILOT_STORAGE_DIR"):
        monkeypatch.delenv(var, raising=False)
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


def prod_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "prod",
        "fingerprint_secret": "k" * 40,
        "database_url": "postgresql+asyncpg://user:pw@db.internal:5432/apilot",
    }
    return make_settings(**(base | overrides))


def test_prod_refuses_the_published_fingerprint_secret() -> None:
    """The secret keys the HMAC over vendor bank details and .env.example ships this
    exact literal in a public repo. Booting on it makes every payment fingerprint
    computable by a stranger and F-110 forgeable, with nothing visible at runtime."""
    with pytest.raises(ValidationError) as caught:
        prod_settings(fingerprint_secret=DEV_FINGERPRINT_SECRET)

    assert "APILOT_FINGERPRINT_SECRET" in str(caught.value)


def test_prod_refuses_a_short_fingerprint_secret() -> None:
    with pytest.raises(ValidationError) as caught:
        prod_settings(fingerprint_secret="too-short")

    assert "shorter than" in str(caught.value)


def test_prod_refuses_the_dev_database_url() -> None:
    with pytest.raises(ValidationError) as caught:
        prod_settings(database_url="postgresql+asyncpg://apilot:apilot@localhost:5433/apilot")

    assert "APILOT_DATABASE_URL" in str(caught.value)


def test_prod_reports_every_problem_at_once() -> None:
    """One restart per misconfiguration is a bad way to learn what else is wrong."""
    with pytest.raises(ValidationError) as caught:
        make_settings(
            env="prod",
            fingerprint_secret=DEV_FINGERPRINT_SECRET,
            database_url="postgresql+asyncpg://apilot:apilot@localhost:5433/apilot",
        )

    message = str(caught.value)
    assert "APILOT_FINGERPRINT_SECRET" in message
    assert "APILOT_DATABASE_URL" in message


def test_prod_accepts_a_real_configuration() -> None:
    assert prod_settings().env == "prod"


@pytest.mark.parametrize("env", ["dev", "test"])
def test_non_prod_keeps_working_defaults(env: str) -> None:
    """The guard must never make local development or CI need a secret."""
    assert make_settings(env=env).fingerprint_secret.get_secret_value() == DEV_FINGERPRINT_SECRET
