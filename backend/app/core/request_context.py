"""Per-request correlation id.

Held in a ``ContextVar`` so any layer — a service, a repository, an exception
handler — can stamp the current request id onto a log line or an error body
without threading the value through every signature.
"""

from __future__ import annotations

from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()
