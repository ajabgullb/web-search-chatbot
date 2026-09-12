"""Typed application settings loaded from the environment.

Every configurable value in the system passes through here. Nothing else in the
codebase may read ``os.environ`` directly — that keeps configuration typed,
documented in ``.env.example``, and validatable at startup.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> backend -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Values shipped in .env.example. Fine for development, never for production.
_INSECURE_SECRETS = frozenset(
    {
        "dev-insecure-change-me-not-for-production",
        "dev-insecure-change-me",
        "changeme",
        "secret",
        "",
    }
)


class Environment(StrEnum):
    development = "development"
    test = "test"
    production = "production"


class Settings(BaseSettings):
    """Application settings.

    Loaded from process environment first, then a local ``.env`` file. Unknown
    keys are ignored so that a shared ``.env`` can also feed Docker Compose.
    """

    model_config = SettingsConfigDict(
        # The single .env lives at the repository root so Compose and the
        # backend read identical values. A backend-local .env, if present,
        # wins — handy for one-off overrides. Missing files are ignored, which
        # is the normal case in containers where env vars are injected.
        env_file=(_REPO_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    environment: Environment = Environment.development
    app_name: str = "Web Search Chatbot"
    api_v1_prefix: str = "/api/v1"
    frontend_origin: str = "http://localhost:5173"
    log_level: str = "INFO"

    # --- Database ---
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://app:app@localhost:5432/app"),
    )
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout_seconds: int = 30

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: PostgresDsn) -> PostgresDsn:
        # The whole stack is async; a sync driver would silently block the event
        # loop rather than fail, so reject it loudly at startup instead.
        if value.scheme != "postgresql+asyncpg":
            raise ValueError(
                f"DATABASE_URL must use the postgresql+asyncpg driver, got {value.scheme!r}"
            )
        return value

    # --- Authentication ---

    jwt_secret: str = "dev-insecure-change-me-not-for-production"  # noqa: S105
    jwt_issuer: str = "web-search-chatbot"
    jwt_audience: str = "web-search-chatbot-api"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    password_reset_ttl_minutes: int = 30
    password_min_length: int = 12
    # Argon2 has no bcrypt-style truncation, so long passphrases are welcome;
    # the ceiling only bounds the work an unauthenticated caller can demand.
    password_max_length: int = 1024

    # --- Conversation and agent budgets ---
    # Enforced server-side when runs land in Phase 6. It exists now because the
    # composer shows a live counter against it, and a limit the client invents
    # for itself is a limit that drifts.
    max_user_message_chars: int = 16_000

    # --- LLM provider ---
    # One OpenAI-compatible implementation covers OpenAI, OpenRouter, Groq, and
    # a local Ollama: only the base URL and model name change.
    llm_provider: str = "openai_compatible"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_timeout_seconds: float = 60.0
    # Bounds the cost and latency of a single answer. Separate from the run
    # deadline, which bounds the whole graph.
    llm_max_output_tokens: int = 1_500
    # Low but not zero: the answer should be stable across retries without
    # reading like a lookup table.
    llm_temperature: float = 0.2

    # --- Search provider ---
    search_provider: str = "tavily"
    search_api_key: str = ""
    search_base_url: str = "https://api.tavily.com"

    # --- Agent budgets ---
    max_searches_per_run: int = 3
    max_results_per_search: int = 5
    search_timeout_seconds: float = 20.0
    run_timeout_seconds: float = 90.0
    # Turns of history handed to the model, newest first. Older turns are
    # dropped before the context budget is reached rather than after.
    max_context_messages: int = 20

    # --- Email ---
    smtp_host: str = ""
    smtp_port: int = 1025
    email_from: str = "no-reply@localhost"

    # --- Rate limits (requests per window, per client IP) ---
    rate_limit_login: int = 10
    rate_limit_register: int = 5
    rate_limit_forgot_password: int = 5
    rate_limit_window_seconds: int = 300

    @field_validator("jwt_secret")
    @classmethod
    def _reject_short_secret(cls, value: str) -> str:
        # RFC 7518 3.2: an HMAC-SHA256 key must be at least as long as the
        # hash output. Shorter keys weaken every token the API issues.
        if len(value) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def _production_fails_closed(self) -> Settings:
        """Refuse to boot production with development defaults.

        Phase 8 extends this; the checks that exist must never be relaxed to
        make an environment start.
        """
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.jwt_secret in _INSECURE_SECRETS or len(self.jwt_secret) < 32:
            problems.append(
                "JWT_SECRET must be a unique random value of at least 32 characters in production"
            )
        if "*" in self.frontend_origin:
            problems.append("FRONTEND_ORIGIN must be an explicit origin, never a wildcard")
        if not self.frontend_origin.startswith("https://"):
            problems.append("FRONTEND_ORIGIN must use https in production")
        # A production API that boots without provider credentials looks healthy
        # and fails on the first user question. Refuse at startup instead.
        if not self.llm_api_key:
            problems.append("LLM_API_KEY is required in production")
        if not self.search_api_key:
            problems.append("SEARCH_API_KEY is required in production")
        if problems:
            raise ValueError("Invalid production configuration: " + "; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.production

    @property
    def cors_origins(self) -> list[str]:
        """Explicit allow-list. Never a wildcard — credentials are in play."""
        return [self.frontend_origin]

    @property
    def auth_cookie_path(self) -> str:
        """Refresh cookie scope.

        Narrow enough that the cookie is not attached to ordinary API calls,
        wide enough to cover both ``/auth/refresh`` and ``/auth/logout``.
        """
        return f"{self.api_v1_prefix}/auth"

    @property
    def auth_cookie_secure(self) -> bool:
        """Browsers drop ``Secure`` cookies on plain http, so development
        needs it off. Every other environment must have it on."""
        return self.environment is not Environment.development


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that importing modules and FastAPI dependencies share one
    validated instance. Tests clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()
