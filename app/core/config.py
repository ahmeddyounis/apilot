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

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shared with .env.example. Named rather than inlined so the production guard below
# and the shipped example can never drift apart.
DEV_FINGERPRINT_SECRET = "change-me-dev-only"
_MIN_FINGERPRINT_SECRET_BYTES = 32
_DEV_DATABASE_URL = "postgresql+asyncpg://apilot:apilot@localhost:5433/apilot"


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
    database_url: str = _DEV_DATABASE_URL
    redis_url: str = "redis://localhost:6380/0"

    # Connection pools and timeouts. Sized against the worker's concurrency: arq
    # runs `worker_max_jobs` coroutines on one loop, each of which may hold a session,
    # so the pool has to cover that with headroom or jobs block on checkout and then
    # fail — and a plain exception in a job is dropped, not retried.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_recycle_s: int = 1800
    db_pool_timeout_s: int = 30
    db_statement_timeout_ms: int = 30_000
    redis_connect_timeout_s: float = 2.0
    redis_socket_timeout_s: float = 5.0

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
    # HNSW candidate-list size. Raised above pgvector default (40) because the search
    # post-filters on model and status, so the scan has to over-fetch to still return
    # ten usable neighbours.
    near_dup_ef_search: int = 100
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
    worker_max_jobs: int = 8
    stale_job_requeue_minutes: int = 15
    stale_batch_size: int = 100

    # Worker metrics exporter. The worker has no HTTP server of its own, and both
    # domain counters are incremented almost entirely inside it — so without this the
    # numbers /metrics serves describe only what the API does directly.
    worker_metrics_enabled: bool = True
    worker_metrics_port: int = 9110
    metrics_refresh_interval_s: int = 15

    # Analytics reporting window when the caller does not supply one. Unbounded
    # aggregates over the whole history get slower every day the system runs, on
    # an endpoint dashboards poll.
    analytics_default_window_days: int = 90

    # Review controls
    # Four-eyes: refuse an approval from the key that uploaded or corrected the
    # invoice. Off by default because a single-operator deployment would deadlock
    # on it — there would be nobody else to approve.
    require_separate_approver: bool = False

    # Fraud fingerprinting
    fingerprint_secret: SecretStr = SecretStr(DEV_FINGERPRINT_SECRET)

    @model_validator(mode="after")
    def _refuse_unsafe_production_config(self) -> "Settings":
        """Refuse to start in prod on defaults that are safe only in dev.

        The fingerprint secret is the sharp one. It keys the HMAC over vendor bank
        details, `.env.example` ships the same literal this defaults to, and the repo
        is public — so a deploy that simply forgets to set it makes every payment
        fingerprint computable by anyone who can read GitHub, and F-110 (bank details
        changed) forgeable. Nothing about that failure is visible at runtime: the
        pipeline keeps working and the fraud signal keeps reporting, it just reports
        something an attacker can predict.

        A crash loop with a legible message is strictly better than serving traffic
        in that state. This runs inside `get_settings()`, which both entrypoints
        reach before serving anything.
        """
        if self.env != "prod":
            return self

        problems: list[str] = []
        secret = self.fingerprint_secret.get_secret_value()
        if secret == DEV_FINGERPRINT_SECRET:
            problems.append(
                "APILOT_FINGERPRINT_SECRET is still the development default from "
                ".env.example, which is published in a public repository"
            )
        elif len(secret.encode()) < _MIN_FINGERPRINT_SECRET_BYTES:
            problems.append(
                f"APILOT_FINGERPRINT_SECRET is shorter than {_MIN_FINGERPRINT_SECRET_BYTES} bytes"
            )
        if self.database_url == _DEV_DATABASE_URL:
            problems.append("APILOT_DATABASE_URL is still the local dev default")

        if problems:
            raise ValueError(
                "Refusing to start with APILOT_ENV=prod:\n"
                + "\n".join(f"  - {problem}" for problem in problems)
                + "\nGenerate a secret with: python -c "
                '"import secrets; print(secrets.token_urlsafe(32))"'
            )
        return self

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
