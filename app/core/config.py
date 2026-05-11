"""
Application configuration.

WHY pydantic-settings:
  - 12-factor: every value comes from the environment (or a `.env` file in dev).
  - Zero hardcoding (master rule §2): URLs, credentials, tunables are typed
    and validated at startup — invalid config fails fast, not in production.
  - A single `Settings` instance is imported everywhere, eliminating ad-hoc
    `os.getenv(...)` calls scattered across the codebase.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """
    Strongly-typed application settings.

    Field names map directly to env var names (case-insensitive). Defaults
    here are *development-safe*; production overrides are supplied via env.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Persistence ------------------------------------------------------
    # SQLAlchemy URL using the psycopg (v3) driver, e.g.
    #   postgresql+psycopg://user:pass@host:5432/dbname
    database_url: str = Field(
        default="postgresql+psycopg://notif:notif@db:5432/notifications",
        description="SQLAlchemy database URL.",
    )

    # --- Broker / result backend -----------------------------------------
    celery_broker_url: str = Field(
        default="redis://redis:6379/0",
        description="Celery broker URL (Redis).",
    )
    celery_result_backend: str = Field(
        default="redis://redis:6379/1",
        description="Celery result backend URL (Redis, separate logical DB).",
    )

    # --- Logging ----------------------------------------------------------
    log_level: LogLevel = Field(default="INFO")
    # JSON in non-dev for log aggregators; human-readable in dev.
    log_json: bool = Field(default=True)

    # --- Retry tunables (consumed in Sprint 4) ---------------------------
    # Surfaced now so the worker module can import them without circulars.
    max_retry_attempts: int = Field(default=5, ge=1, le=20)
    retry_backoff_base_seconds: int = Field(default=2, ge=1, le=60)

    # --- Mock provider tunables ------------------------------------------
    # Probabilities live in config (not constants) so tests can pin them
    # to 0.0 for deterministic runs and chaos tests can crank them up.
    # Each value is an independent probability evaluated per attempt.
    email_transient_failure_rate: float = Field(
        default=0.10, ge=0.0, le=1.0,
        description="P(retryable transient error) per email send attempt.",
    )
    email_bounce_rate: float = Field(
        default=0.05, ge=0.0, le=1.0,
        description="P(hard bounce, non-retryable) per email send attempt.",
    )
    email_open_rate: float = Field(
        default=0.30, ge=0.0, le=1.0,
        description="P(simulated 'opened' event) given successful delivery.",
    )

    # SMS — character limit chosen to match a single GSM-7 segment.
    sms_max_chars: int = Field(default=160, ge=1, le=10_000)
    sms_transient_failure_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    sms_failed_rate: float = Field(default=0.02, ge=0.0, le=1.0)

    # Push.
    push_transient_failure_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    push_invalid_token_rate: float = Field(default=0.03, ge=0.0, le=1.0)
    push_clicked_rate: float = Field(default=0.20, ge=0.0, le=1.0)

    # Webhook (real HTTP POST against the configured URL).
    webhook_request_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    # SSRF guard: block webhook URLs that resolve to private / loopback /
    # link-local / reserved / multicast IP literals. Defaults to True
    # (operators can disable for dev/loopback testing). Defends against a
    # malicious user setting webhook_url to e.g. http://169.254.169.254/
    # (AWS IMDS), http://127.0.0.1/, or RFC1918 internal services.
    webhook_block_private_addresses: bool = Field(default=True)
    # Follow redirects? Disabled by default — a 30x to a private IP would
    # bypass the validation that we did on the original URL.
    webhook_follow_redirects: bool = Field(default=False)

    # --- Frequency caps & scheduling (Sprint 4) --------------------------
    # Redis URL for the rate-limiter counters. Defaults to the same node as
    # the Celery broker but a separate logical DB so cap state cannot
    # collide with task payloads.
    rate_limiter_redis_url: str = Field(
        default="redis://redis:6379/2",
        description="Redis URL for frequency-cap counters.",
    )
    # If True, HIGH-priority notifications bypass frequency caps entirely.
    # Documented in DECISIONS.md §3 step 6.
    #
    # Default flipped to False (was True): combined with quiet-hours bypass,
    # an unconstrained HIGH bypass turns any caller able to set
    # priority=HIGH into an unbounded-throughput firehose. Operators who
    # truly want this — and who control who can mint HIGH — can re-enable.
    frequency_cap_high_priority_bypass: bool = Field(default=False)
    # Behavior when the Redis cap-counter store is unreachable.
    #   True  → fail OPEN (allow the send, log error). Reasonable for
    #           best-effort marketing.
    #   False → fail CLOSED (drop the send, mark FAILED). Safer for a
    #           service that also carries operational alerts where one
    #           Redis blip should not lift caps for every user at once.
    # Default flipped to False; operators can opt back into fail-open per
    # their risk profile.
    rate_limiter_fail_open: bool = Field(default=False)
    # Global hard ceilings used as fallback when a user has no per-window
    # cap configured. `0` disables the fallback (no implicit cap).
    default_frequency_cap_per_hour: int = Field(default=0, ge=0)
    default_frequency_cap_per_day: int = Field(default=0, ge=0)

    # How often the scheduled-notifications scanner runs (Celery Beat).
    scheduled_scan_interval_seconds: int = Field(default=30, ge=5, le=3600)
    # Safety guard: number of due notifications dispatched per scan tick.
    # Bounds memory + DB pressure in case of a backlog spike.
    scheduled_scan_batch_size: int = Field(default=200, ge=1, le=10_000)

    # --- Worker time-limits ----------------------------------------------
    # `soft` raises SoftTimeLimitExceeded inside the task (autoretry can
    # then handle it gracefully); `hard` SIGKILLs the worker child if even
    # cleanup hangs. `hard` MUST exceed `soft` (validated below).
    task_soft_time_limit_seconds: int = Field(default=30, ge=1, le=3600)
    task_hard_time_limit_seconds: int = Field(default=60, ge=2, le=7200)

    @model_validator(mode="after")
    def _hard_must_exceed_soft(self) -> "Settings":
        if self.task_hard_time_limit_seconds <= self.task_soft_time_limit_seconds:
            raise ValueError(
                "task_hard_time_limit_seconds must be strictly greater than "
                "task_soft_time_limit_seconds (give the soft handler time to "
                "run before SIGKILL)."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Process-wide singleton accessor.

    WHY `lru_cache`: building `Settings` parses env + .env on every call.
    Caching it avoids repeated I/O and guarantees a single source of truth.
    Tests can clear the cache via `get_settings.cache_clear()`.
    """
    return Settings()
