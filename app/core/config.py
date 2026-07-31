"""Application settings.

Everything configurable lives here, read from the environment with the ``APILOT_``
prefix (plus a bare ``ANTHROPIC_API_KEY`` alias, since that name is the ecosystem
convention). Defaults target local dev against docker-compose, which publishes
Postgres on host port 5433 and Redis on 6380 — 5432/6379 are occupied on this machine.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Runtime
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # Infrastructure
    database_url: str = "postgresql+asyncpg://apilot:apilot@localhost:5433/apilot"
    redis_url: str = "redis://localhost:6380/0"

    # Upload storage
    storage_dir: Path = Path("./storage/uploads")
    max_upload_bytes: int = 15 * 1024 * 1024
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/tiff",
        ]
    )

    # Decision gate & screening thresholds
    auto_approve_max_total: Decimal = Decimal("2500.00")
    near_dup_cosine_threshold: float = 0.85
    dup_suspect_threshold: float = 0.80
    round_number_min_total: Decimal = Decimal("500.00")
    confidence_review_floor: float = 0.70
    price_tolerance_pct: Decimal = Decimal("0.01")
    qty_tolerance_pct: Decimal = Decimal("0")
    supported_currencies: list[str] = Field(
        default_factory=lambda: (
            "USD EUR GBP CAD AUD JPY CHF SEK NOK DKK NZD SGD HKD AED SAR INR MXN BRL PLN CZK"
        ).split()
    )

    # Rate limiting
    rate_limit_enabled: bool = True
    rate_limit_default_per_min: int = 120
    rate_limit_ingest_per_min: int = 30

    # LLM
    llm_mode: Literal["auto", "live", "fake"] = "auto"
    extraction_model: str = "claude-opus-5"
    matching_model: str = "claude-opus-5"
    llm_max_concurrency: int = 4
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key"),
    )

    # Embeddings (near-duplicate detection)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # Jobs & idempotency
    idempotency_ttl_hours: int = 24
    stage_timeout_extraction_s: int = 180
    stage_timeout_matching_s: int = 180
    pipeline_max_tries: int = 4
    stale_job_requeue_minutes: int = 15

    # Fraud fingerprinting
    fingerprint_secret: SecretStr = SecretStr("change-me-dev-only")

    @property
    def resolved_llm_mode(self) -> Literal["live", "fake"]:
        """``auto`` resolves to live only when a real key is present.

        The app must boot and run fully offline (tests, CI) with an empty env.
        """
        if self.llm_mode == "fake":
            return "fake"
        has_key = self.anthropic_api_key is not None and bool(
            self.anthropic_api_key.get_secret_value().strip()
        )
        if self.llm_mode == "live":
            return "live"
        return "live" if has_key else "fake"


@lru_cache
def get_settings() -> Settings:
    return Settings()
