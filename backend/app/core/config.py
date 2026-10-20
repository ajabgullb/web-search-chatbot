"""Typed application settings loaded from the environment.

Every configurable value in the system passes through here. Nothing else in the
codebase may read ``os.environ`` directly — that keeps configuration typed,
documented in ``.env.example``, and validatable at startup.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> backend -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Database passwords that ship in examples and tutorials.
_WEAK_DATABASE_PASSWORDS = frozenset({"", "app", "postgres", "password", "changeme", "secret"})

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


PRODUCTION_CONFIG_ERROR = "Refusing to start in production:\n"

# The width of the ``document_chunks.embedding`` column. pgvector stores only
# vectors of exactly this length, so EMBEDDING_DIMENSIONS must match it, and
# changing either one is a migration plus a re-embed, never a config edit.
EMBEDDING_VECTOR_DIMENSIONS = 1536


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
    # JSON for anything a machine reads (every deployed environment); console
    # is a readable single line for a developer's terminal. Both redact.
    log_format: Literal["json", "console"] = "json"

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

    # --- Redis ---
    # Empty selects in-process coordination, which is correct for exactly one
    # API process. Production refuses to start without it.
    redis_url: str = ""
    # A Redis that hangs must fail a request quickly, not stall it; the
    # database still guards correctness while Redis is away.
    redis_timeout_seconds: float = 2.0

    # --- Observability ---
    # Bearer token a Prometheus scraper must present at /metrics. Empty in
    # production closes the endpoint; empty in development leaves it open.
    metrics_token: str = ""
    # Optional LangSmith tracing. Inputs and outputs are never uploaded; see
    # app/agent/tracing.py. The endpoint is for the EU region or self-hosting.
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "web-search-chatbot"
    langsmith_endpoint: str = ""

    # --- Request limits ---
    # Sized so any valid message fits however it is encoded. The worst case is
    # a 16,000-character message of emoji with ASCII-only JSON escaping
    # (Python's default): 12 bytes each, about 192 KB. Anything bigger is
    # refused before it is buffered or parsed.
    max_request_body_bytes: int = 262_144

    # --- Conversation and agent budgets ---
    # Enforced by the run service. The composer shows a live counter against
    # the same value, so the client never invents a limit of its own.
    max_user_message_chars: int = 16_000

    # --- Streaming ---
    # A comment line sent while the agent is quiet (waiting on a search, say).
    # Proxies and load balancers close idle connections, typically after 30-60s;
    # well under that keeps the stream alive through them.
    sse_heartbeat_seconds: float = 15.0
    # How long a run may sit in queued/running before it is presumed dead —
    # its process crashed or was killed — and stops blocking its conversation.
    # Must exceed the run deadline, or a healthy run could be declared dead.
    run_stale_grace_seconds: float = 60.0
    # How long a run whose stream dropped keeps going while nobody watches.
    # Long enough for a page reload to reconnect via GET /runs/{id}; short
    # enough that a closed tab stops spending almost at once.
    run_abandon_grace_seconds: float = 10.0
    # On shutdown, how long in-flight runs may keep going before they are
    # stopped and recorded as interrupted (retryable). Uvicorn has usually
    # drained runs with an open stream before this starts; this covers runs
    # whose client is reconnecting through GET /runs/{id}.
    shutdown_drain_seconds: float = 10.0

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

    # --- Embedding provider (ADR-0015) ---
    # Configured separately from the chat model on purpose: LLM_BASE_URL may
    # name a provider with no embeddings API at all, and the two must be free
    # to change independently. Empty reuses LLM_API_KEY when both base URLs
    # are on the same host; see ``effective_embedding_api_key``.
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"
    # OpenAI's cheapest embedding model when chosen (2026-10-19): $0.02 per 1M
    # tokens, against $0.10 for ada-002 and $0.13 for text-embedding-3-large.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = EMBEDDING_VECTOR_DIMENSIONS
    # One request. A full batch of chunks takes longer than a single query.
    embedding_timeout_seconds: float = 30.0
    # Chunks per request. OpenAI caps a request at 300,000 tokens across all
    # inputs; 100 chunks of ~500 tokens stays far below that.
    embedding_batch_size: int = Field(default=100, ge=1, le=500)

    # --- Documents (ADR-0015) ---
    # A 10 MiB PDF is a long report; anything bigger is usually scans, which
    # carry no text to read anyway.
    max_document_bytes: int = Field(default=10_485_760, ge=1)
    max_document_pages: int = Field(default=200, ge=1)
    # Extracted text. About 125,000 tokens, roughly $0.0025 to embed.
    max_document_chars: int = Field(default=500_000, ge=1)
    max_chunks_per_document: int = Field(default=400, ge=1)
    max_documents_per_conversation: int = Field(default=5, ge=1)
    # Parsing runs in child processes (app/services/pdf.py). Each one may use
    # this much address space and this long before it is killed.
    document_parse_workers: int = Field(default=2, ge=1)
    document_parse_timeout_seconds: float = Field(default=30.0, gt=0)
    document_parse_memory_mb: int = Field(default=1024, ge=128)
    # Uploads per user per window. Each one costs parse time and embeddings.
    rate_limit_document_uploads: int = 10
    rate_limit_document_uploads_window_seconds: int = 600
    # Embedding tokens per user in any rolling 24 hours, uploads and queries
    # together. 2,000,000 tokens of text-embedding-3-small is about $0.04.
    daily_embedding_token_limit_per_user: int = Field(default=2_000_000, ge=1)
    # Background embedding (app/services/ingestion.py). A worker owns a
    # document for one lease, renewed after every batch; a crashed worker's
    # document is picked up by the sweep once the lease runs out.
    document_embed_lease_seconds: float = Field(default=120.0, gt=0)
    document_embed_max_attempts: int = Field(default=3, ge=1)
    document_ingestion_sweep_seconds: float = Field(default=30.0, gt=0)

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
    smtp_username: str = ""
    smtp_password: str = ""
    # none: plaintext (Mailpit only). starttls: upgrade on connect, usually
    # port 587. tls: implicit TLS from the first byte, usually port 465.
    # Production refuses "none": a reset email carries a working credential.
    smtp_security: Literal["none", "starttls", "tls"] = "none"
    smtp_timeout_seconds: float = 10.0
    email_from: str = "no-reply@localhost"

    # --- Rate limits (requests per window, per client IP) ---
    rate_limit_login: int = 10
    rate_limit_register: int = 5
    rate_limit_forgot_password: int = 5
    # The same endpoints limited per *account* (by email), over the same
    # window. Per-IP limits do nothing against a botnet spreading attempts on
    # one account across thousands of addresses; these do.
    rate_limit_login_per_account: int = 20
    rate_limit_forgot_password_per_account: int = 3
    rate_limit_window_seconds: int = 300
    # Runs are the billable path, so they are limited per *user* rather than
    # per IP: an authenticated caller behind a shared NAT must not starve
    # others, and one account must not spend unbounded provider budget.
    rate_limit_runs: int = 20
    rate_limit_runs_window_seconds: int = 60
    # Runs started per user in any rolling 24 hours. The per-minute limit stops
    # bursts; a patient script staying just under it could still start ~28,800
    # runs a day. This caps what one account can spend, counted from the runs
    # table itself, so it holds across replicas and survives a Redis restart.
    daily_run_limit_per_user: int = Field(default=100, ge=1)
    # Verification emails one account may request per rate-limit window.
    # Bounds how often the app can be made to mail a single address.
    rate_limit_resend_verification: int = 3

    # --- Email verification ---
    # Unverified accounts can sign in and organise conversations but cannot
    # start runs: a run spends provider money, and accounts on addresses
    # nobody controls are otherwise free to mint. Production refuses false.
    require_email_verification: bool = True
    email_verification_ttl_hours: int = Field(default=48, ge=1)

    @field_validator("jwt_secret")
    @classmethod
    def _reject_short_secret(cls, value: str) -> str:
        # RFC 7518 3.2: an HMAC-SHA256 key must be at least as long as the
        # hash output. Shorter keys weaken every token the API issues.
        if len(value) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("embedding_dimensions")
    @classmethod
    def _match_vector_column(cls, value: int) -> int:
        # A mismatch would only surface as an insert error on the first upload.
        if value != EMBEDDING_VECTOR_DIMENSIONS:
            raise ValueError(
                f"EMBEDDING_DIMENSIONS must be {EMBEDDING_VECTOR_DIMENSIONS}, the width of the "
                "stored vectors; changing it needs a migration and a re-embed"
            )
        return value

    @model_validator(mode="after")
    def _production_fails_closed(self) -> Settings:
        """Refuse to boot production with development defaults.

        Checks may be added; the ones that exist must never be relaxed to make
        an environment start.
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
        if not self.effective_embedding_api_key:
            problems.append(
                "EMBEDDING_API_KEY is required in production (PDF documents) unless "
                "LLM_BASE_URL is on the same host as EMBEDDING_BASE_URL, whose key is then reused"
            )
        # Without it, run locks and rate limits are per process: each replica
        # would coordinate only with itself.
        if not self.redis_url:
            problems.append("REDIS_URL is required in production")
        if not self.require_email_verification:
            problems.append(
                "REQUIRE_EMAIL_VERIFICATION must be true in production: unverified "
                "accounts would spend provider budget"
            )
        if self.langsmith_tracing and not self.langsmith_api_key:
            problems.append("LANGSMITH_API_KEY is required when LANGSMITH_TRACING is true")
        if self.metrics_token and len(self.metrics_token) < 32:
            problems.append("METRICS_TOKEN must be at least 32 characters when set")
        if (self.database_url.hosts()[0].get("password") or "") in _WEAK_DATABASE_PASSWORDS:
            problems.append("DATABASE_URL must use a strong database password in production")
        # Without SMTP, reset emails are logged as suppressed and never sent:
        # users who forget their password are locked out.
        if not self.smtp_host:
            problems.append("SMTP_HOST is required in production (password-reset email)")
        if self.smtp_security == "none":
            problems.append(
                "SMTP_SECURITY must be starttls or tls in production: reset links are credentials"
            )
        # Development conveniences that are wrong for production.
        if self.log_level.upper() == "DEBUG":
            problems.append("LOG_LEVEL must not be DEBUG in production")
        if self.log_format != "json":
            problems.append("LOG_FORMAT must be json in production")
        if problems:
            raise ValueError(PRODUCTION_CONFIG_ERROR + "\n".join(f"  - {p}" for p in problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.production

    @property
    def run_lock_lease_seconds(self) -> float:
        """How long a conversation's run lock lives if nobody releases it.

        The same bound the stale-run sweep uses: a healthy run always finishes
        (and releases) well inside it, and a process that dies holding the lock
        blocks the conversation no longer than its dead run row already would.
        """
        return self.run_timeout_seconds + self.run_stale_grace_seconds

    @property
    def effective_embedding_api_key(self) -> str:
        """The key embedding requests are sent with.

        ``EMBEDDING_API_KEY`` when set. Otherwise ``LLM_API_KEY``, but only
        when the chat endpoint is on the same host as the embeddings endpoint
        (both OpenAI, by default): then it is the same provider's key. With a
        chat model elsewhere (Groq, OpenRouter), reusing its key would hand
        one provider's credential to another, so there is no fallback.
        """
        if self.embedding_api_key:
            return self.embedding_api_key
        same_host = (
            urlsplit(self.llm_base_url).hostname == urlsplit(self.embedding_base_url).hostname
        )
        return self.llm_api_key if same_host else ""

    @property
    def max_upload_body_bytes(self) -> int:
        """Body limit for the upload route: the file plus multipart framing."""
        return self.max_document_bytes + 64 * 1024

    @property
    def expose_api_docs(self) -> bool:
        """Swagger UI and the raw schema are for developers. In production
        they are a map of every endpoint, handed to anyone who asks."""
        return not self.is_production

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
