"""SMTP delivery: credentials and transport security reach the relay."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.providers import email
from app.providers.email import EmailPayload, SmtpEmailSender

PAYLOAD = EmailPayload(to="user@example.com", subject="Reset", body="https://x/reset?token=t")


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_send(message: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(email.aiosmtplib, "send", fake_send)
    return calls


@pytest.mark.parametrize(
    ("security", "use_tls", "start_tls"),
    [("none", False, False), ("starttls", False, True), ("tls", True, False)],
)
async def test_the_configured_security_mode_is_used(
    sent: list[dict[str, Any]], security: str, use_tls: bool, start_tls: bool
) -> None:
    settings = Settings(smtp_host="smtp.example.com", smtp_port=587, smtp_security=security)  # type: ignore[arg-type]

    await SmtpEmailSender(settings).send(PAYLOAD)

    [call] = sent
    assert call["use_tls"] is use_tls
    assert call["start_tls"] is start_tls
    assert call["hostname"] == "smtp.example.com"
    assert call["port"] == 587


async def test_credentials_are_passed_when_configured(sent: list[dict[str, Any]]) -> None:
    settings = Settings(
        smtp_host="smtp.example.com",
        smtp_security="starttls",
        smtp_username="mailer",
        smtp_password="relay-password",
    )

    await SmtpEmailSender(settings).send(PAYLOAD)

    assert sent[0]["username"] == "mailer"
    assert sent[0]["password"] == "relay-password"


async def test_no_credentials_means_no_login(sent: list[dict[str, Any]]) -> None:
    """An empty username must not become an attempted login as ''."""
    await SmtpEmailSender(Settings(smtp_host="mailpit")).send(PAYLOAD)

    assert sent[0]["username"] is None
    assert sent[0]["password"] is None


@pytest.mark.parametrize(
    "failure",
    [OSError("connection refused"), email.aiosmtplib.SMTPServerDisconnected("gone")],
)
async def test_transport_failures_surface_as_delivery_errors(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Callers catch one provider-neutral error, not SMTP library types."""

    async def failing_send(message: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(email.aiosmtplib, "send", failing_send)
    sender = SmtpEmailSender(Settings(smtp_host="mail.example.com"))
    with pytest.raises(email.EmailDeliveryError):
        await sender.send(PAYLOAD)
