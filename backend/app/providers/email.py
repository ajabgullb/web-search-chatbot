"""Outbound email, behind an interface.

Two implementations ship today: SMTP (Mailpit locally, a real relay later) and
a logging fallback for when no host is configured. Tests substitute a
recording double. Nothing above this module knows which one is in use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import aiosmtplib

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmailPayload:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, payload: EmailPayload) -> None: ...


class SmtpEmailSender:
    """Sends through an SMTP relay. Points at Mailpit in development."""

    def __init__(self, settings: Settings) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._sender = settings.email_from
        self._username = settings.smtp_username or None
        self._password = settings.smtp_password or None
        self._security = settings.smtp_security
        self._timeout = settings.smtp_timeout_seconds

    async def send(self, payload: EmailPayload) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = payload.to
        message["Subject"] = payload.subject
        message.set_content(payload.body)

        # Plaintext only for Mailpit in development; production settings
        # refuse it. Certificates are always verified when TLS is on. Failures
        # propagate to the caller, which decides whether the operation is
        # critical.
        await aiosmtplib.send(
            message,
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            use_tls=self._security == "tls",
            start_tls=self._security == "starttls",
            timeout=self._timeout,
        )


class LoggingEmailSender:
    """Fallback when SMTP_HOST is unset: record that a mail was due.

    The body is not logged — a reset email contains a working credential.
    """

    async def send(self, payload: EmailPayload) -> None:
        # No SMTP_HOST configured. The body holds the reset link, so only the
        # subject is logged.
        logger.info("email_suppressed", extra={"subject": payload.subject})


def build_email_sender(settings: Settings) -> EmailSender:
    return SmtpEmailSender(settings) if settings.smtp_host else LoggingEmailSender()
