"""Test doubles for the provider interfaces."""

from __future__ import annotations

from app.providers.email import EmailPayload


class RecordingEmailSender:
    """Captures messages instead of sending them.

    Lets a test read the reset token out of the email body, which is the only
    place it ever exists in plaintext — the database stores just its hash.
    """

    def __init__(self) -> None:
        self.sent: list[EmailPayload] = []

    async def send(self, payload: EmailPayload) -> None:
        self.sent.append(payload)

    @property
    def last(self) -> EmailPayload:
        assert self.sent, "no email was sent"
        return self.sent[-1]
