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
