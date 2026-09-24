"""Production fails closed, as a real process start.

Each test imports ``app.main`` in a fresh interpreter, which is exactly what
uvicorn does in the container, with the environment a deployment would give
it. Every variable the checks read is set explicitly, so a developer's own
``.env`` cannot change the outcome.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Environment, Settings
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Distinctive values, so a leak into the error output is easy to spot.
LLM_KEY = "sk-leakcheck-0123456789abcdef"
DB_PASSWORD = "db-leakcheck-password-0123456789"

VALID_PRODUCTION = {
    "ENVIRONMENT": "production",
    "JWT_SECRET": "p" * 48,
    "FRONTEND_ORIGIN": "https://chat.example.com",
    "DATABASE_URL": f"postgresql+asyncpg://app:{DB_PASSWORD}@db:5432/app",
    "REDIS_URL": "redis://redis:6379/0",
    "LLM_API_KEY": LLM_KEY,
    "SEARCH_API_KEY": "tvly-leakcheck-0123456789",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_SECURITY": "starttls",
    "LOG_LEVEL": "INFO",
    "LOG_FORMAT": "json",
    "METRICS_TOKEN": "",
}


def boot(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **VALID_PRODUCTION, **overrides}
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_valid_production_configuration_starts() -> None:
    result = boot()
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("override", "complaint"),
    [
        ({"JWT_SECRET": "dev-insecure-change-me-not-for-production"}, "JWT_SECRET"),
        ({"FRONTEND_ORIGIN": "*"}, "wildcard"),
        ({"FRONTEND_ORIGIN": "http://chat.example.com"}, "https"),
        ({"LLM_API_KEY": ""}, "LLM_API_KEY"),
        ({"SEARCH_API_KEY": ""}, "SEARCH_API_KEY"),
        ({"REDIS_URL": ""}, "REDIS_URL"),
        ({"DATABASE_URL": "postgresql+asyncpg://app:app@db:5432/app"}, "database password"),
        ({"SMTP_HOST": ""}, "SMTP_HOST"),
        ({"SMTP_SECURITY": "none"}, "SMTP_SECURITY"),
        ({"LOG_LEVEL": "DEBUG"}, "DEBUG"),
    ],
)
def test_a_bad_setting_stops_the_process_with_a_clear_message(
    override: dict[str, str], complaint: str
) -> None:
    result = boot(**override)

    assert result.returncode != 0
    assert complaint in result.stderr
    # A message, not a traceback.
    assert "Traceback" not in result.stderr
    assert result.stderr.startswith("Invalid configuration.")


def test_the_error_never_echoes_configured_secrets() -> None:
    """Pydantic's default error embeds the rejected input: the environment,
    secrets included. The startup message must name problems, not values."""
    result = boot(JWT_SECRET="short", REDIS_URL="")

    assert result.returncode != 0
    for secret in (LLM_KEY, DB_PASSWORD, "tvly-leakcheck"):
        assert secret not in result.stderr
        assert secret not in result.stdout


async def test_production_does_not_serve_api_docs() -> None:
    settings = Settings(
        environment=Environment.production,
        **{key.lower(): value for key, value in VALID_PRODUCTION.items() if key != "ENVIRONMENT"},
    )
    app = create_app(settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as client:
        docs = await client.get("/api/v1/docs")
        schema = await client.get("/api/v1/openapi.json")

    assert docs.status_code == 404
    assert schema.status_code == 404
