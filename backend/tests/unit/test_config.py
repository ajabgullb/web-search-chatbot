"""Settings validation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings


def test_rejects_synchronous_database_driver() -> None:
    """A sync driver would block the event loop instead of failing loudly."""
    with pytest.raises(ValidationError, match="asyncpg"):
        Settings(database_url="postgresql://app:app@localhost:5432/app")  # type: ignore[arg-type]


def test_accepts_async_database_driver() -> None:
    settings = Settings(database_url="postgresql+asyncpg://app:app@db:5432/app")  # type: ignore[arg-type]
    assert settings.database_url.scheme == "postgresql+asyncpg"


def test_cors_origins_never_wildcard() -> None:
    """Credentialed CORS plus a wildcard origin is a security hole."""
    settings = Settings(frontend_origin="http://localhost:5173")
    assert settings.cors_origins == ["http://localhost:5173"]
    assert "*" not in settings.cors_origins


def production_settings(**overrides: object) -> Settings:
    """A production configuration that passes every check, for tests to spoil."""
    values: dict[str, object] = {
        "environment": Environment.production,
        "jwt_secret": "a" * 40,
        "frontend_origin": "https://app.example.com",
        "llm_api_key": "sk-not-a-real-key",
        "search_api_key": "tvly-not-a-real-key",
        "redis_url": "redis://redis:6379/0",
        "database_url": "postgresql+asyncpg://app:a-long-random-password@db:5432/app",
        "smtp_host": "smtp.example.com",
        "log_format": "json",
        "log_level": "INFO",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_is_production_flag() -> None:
    assert production_settings().is_production is True
    assert Settings(environment=Environment.development).is_production is False


def test_production_rejects_the_development_secret() -> None:
    """Fail closed: a default secret must stop the process, not warn."""
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        production_settings(jwt_secret="dev-insecure-change-me-not-for-production")


def test_production_requires_provider_credentials() -> None:
    """An API that boots without them looks healthy and fails on the first
    question a user asks. Refuse at startup instead."""
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        production_settings(llm_api_key="")
    with pytest.raises(ValidationError, match="SEARCH_API_KEY"):
        production_settings(search_api_key="")


def test_production_requires_redis() -> None:
    """Without it every replica would lock and rate-limit only against itself."""
    with pytest.raises(ValidationError, match="REDIS_URL"):
        production_settings(redis_url="")


@pytest.mark.parametrize(
    ("override", "complaint"),
    [
        ({"database_url": "postgresql+asyncpg://app:app@db:5432/app"}, "database password"),
        ({"smtp_host": ""}, "SMTP_HOST"),
        ({"log_level": "debug"}, "DEBUG"),
        ({"log_format": "console"}, "LOG_FORMAT"),
        ({"metrics_token": "short"}, "METRICS_TOKEN"),
    ],
)
def test_production_rejects_development_conveniences(
    override: dict[str, object], complaint: str
) -> None:
    with pytest.raises(ValidationError, match=complaint):
        production_settings(**override)


def test_production_lists_every_problem_at_once() -> None:
    """Fixing one problem per deploy attempt is a slow way to find five."""
    with pytest.raises(ValidationError) as caught:
        production_settings(jwt_secret="changeme" * 5, redis_url="", smtp_host="")
    message = str(caught.value)
    assert "REDIS_URL" in message
    assert "SMTP_HOST" in message


def test_api_docs_are_hidden_in_production() -> None:
    assert production_settings().expose_api_docs is False
    assert Settings(environment=Environment.development).expose_api_docs is True


def test_development_does_not_require_redis() -> None:
    assert Settings(environment=Environment.development, redis_url="").redis_url == ""


def test_run_lock_outlives_the_run_deadline() -> None:
    """A lease shorter than a run could expire mid-run and admit a second one."""
    settings = Settings(run_timeout_seconds=90, run_stale_grace_seconds=60)
    assert settings.run_lock_lease_seconds == 150
    assert settings.run_lock_lease_seconds > settings.run_timeout_seconds


def test_development_does_not_require_provider_credentials() -> None:
    """Phases 1-4 of this repository run with no provider keys at all."""
    assert Settings(environment=Environment.development, llm_api_key="").llm_api_key == ""


def test_production_rejects_a_non_https_or_wildcard_origin() -> None:
    with pytest.raises(ValidationError, match="https"):
        production_settings(frontend_origin="http://app.example.com")
    with pytest.raises(ValidationError, match="wildcard"):
        production_settings(frontend_origin="https://*.example.com")


def test_short_jwt_secret_is_rejected_everywhere() -> None:
    """RFC 7518 requires an HS256 key at least as long as the hash output."""
    with pytest.raises(ValidationError, match="32 characters"):
        Settings(jwt_secret="too-short")


def test_refresh_cookie_is_secure_outside_development() -> None:
    assert Settings(environment=Environment.development).auth_cookie_secure is False
    assert Settings(environment=Environment.test).auth_cookie_secure is True


def test_refresh_cookie_path_is_scoped_to_auth() -> None:
    assert Settings(api_v1_prefix="/api/v1").auth_cookie_path == "/api/v1/auth"


class TestClientMirroredLimits:
    """The frontend keeps its own copy of the message limit for the counter.

    A copy that drifts is worse than no copy: the composer would happily accept
    a message the API then rejects. This pins the two together.
    """

    def test_the_composer_limit_matches_the_setting(self) -> None:
        constants = (
            Path(__file__).resolve().parents[3]
            / "frontend"
            / "src"
            / "features"
            / "chat"
            / "constants.ts"
        )
        source = constants.read_text(encoding="utf-8")

        match = re.search(r"MAX_MESSAGE_CHARACTERS\s*=\s*([\d_]+)", source)
        assert match is not None, "MAX_MESSAGE_CHARACTERS is no longer declared"

        assert int(match.group(1).replace("_", "")) == Settings().max_user_message_chars
