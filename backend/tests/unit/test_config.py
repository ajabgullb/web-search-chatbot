"""Settings validation."""

from __future__ import annotations

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


def test_is_production_flag() -> None:
    valid_production = Settings(
        environment=Environment.production,
        jwt_secret="a" * 40,
        frontend_origin="https://app.example.com",
    )
    assert valid_production.is_production is True
    assert Settings(environment=Environment.development).is_production is False


def test_production_rejects_the_development_secret() -> None:
    """Fail closed: a default secret must stop the process, not warn."""
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment=Environment.production, frontend_origin="https://app.example.com")


def test_production_rejects_a_non_https_or_wildcard_origin() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(
            environment=Environment.production,
            jwt_secret="a" * 40,
            frontend_origin="http://app.example.com",
        )
    with pytest.raises(ValidationError, match="wildcard"):
        Settings(
            environment=Environment.production,
            jwt_secret="a" * 40,
            frontend_origin="https://*.example.com",
        )


def test_short_jwt_secret_is_rejected_everywhere() -> None:
    """RFC 7518 requires an HS256 key at least as long as the hash output."""
    with pytest.raises(ValidationError, match="32 characters"):
        Settings(jwt_secret="too-short")


def test_refresh_cookie_is_secure_outside_development() -> None:
    assert Settings(environment=Environment.development).auth_cookie_secure is False
    assert Settings(environment=Environment.test).auth_cookie_secure is True


def test_refresh_cookie_path_is_scoped_to_auth() -> None:
    assert Settings(api_v1_prefix="/api/v1").auth_cookie_path == "/api/v1/auth"
