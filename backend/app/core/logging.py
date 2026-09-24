"""Structured logging with correlation fields and redaction.

Every line is one JSON object (or, with ``LOG_FORMAT=console``, one readable
line). Call sites log a stable event name and pass fields explicitly:

    logger.info("run_accepted", extra={"retry": False})

and the formatter adds what the call site should not have to repeat: the
request id, and whatever :func:`bind_log_context` has attached to the current
request or run (an opaque user reference, the conversation and run ids).

Redaction is a safety net, not the policy. The policy is that call sites never
log prompts, message bodies, page content, credentials, or tokens. The net
catches what slips through: fields named like secrets or content are masked,
and credential-shaped text in messages and tracebacks (bearer tokens, JWTs,
provider keys, emails) is scrubbed before anything is written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.request_context import get_request_id

_log_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)

REDACTED = "[REDACTED]"

# Attributes every LogRecord has; anything else on a record came from `extra`.
_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)

# Field names whose values are never written, whatever they contain.
_SENSITIVE_FIELDS = frozenset(
    {
        "password",
        "new_password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "secret",
        "jwt_secret",
        # Content, not credentials, but equally never logged by default.
        "content",
        "prompt",
        "messages",
        "answer",
        "snippet",
        "body",
        "query",
        "queries",
    }
)

_TEXT_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), REDACTED),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie)"
            r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
        ),
        rf"\1\2{REDACTED}",
    ),
    # OpenAI-style, Tavily, and LangSmith key shapes.
    (re.compile(r"\b(?:sk|tvly|lsv2)[-_][A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _TEXT_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def user_ref(user_id: uuid.UUID) -> str:
    """An opaque, stable reference to a user for log lines.

    Unkeyed on purpose: someone who reads logs cannot turn it back into an
    account id, but support staff holding an id can compute it and search.
    """
    return hashlib.sha256(user_id.bytes).hexdigest()[:16]


def bind_log_context(**fields: Any) -> None:
    """Attach fields to every later log line in this request or task.

    Context variables are copied into tasks at creation, so a run's task
    inherits the request's fields and adds its own without leaking them back.
    """
    current = _log_context.get() or {}
    _log_context.set({**current, **{k: v for k, v in fields.items() if v is not None}})


def clear_log_context() -> None:
    _log_context.set(None)


def _scrub(key: str, value: Any) -> Any:
    if key.lower() in _SENSITIVE_FIELDS:
        return REDACTED
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, uuid.UUID | datetime):
        return str(value)
    return value


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    request_id = get_request_id()
    if request_id:
        fields["request_id"] = request_id
    fields.update(_log_context.get() or {})
    for key, value in vars(record).items():
        if key not in _RECORD_ATTRIBUTES and not key.startswith("_"):
            fields[key] = value
    return {key: _scrub(key, value) for key, value in fields.items()}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact(record.getMessage()),
        }
        payload.update(_fields(record))
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """One readable line per record, same fields and redaction as JSON."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        fields = " ".join(f"{key}={value}" for key, value in _fields(record).items())
        line = f"{timestamp} {record.levelname:<7} {redact(record.getMessage())} {fields}".rstrip()
        if record.exc_info and record.exc_info[0] is not None:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def setup_logging(level: str, *, log_format: str = "json") -> None:
    """Configure the root logger idempotently."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Re-running (reload, tests) must not stack duplicate handlers.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ConsoleFormatter() if log_format == "console" else JsonFormatter())
    root.addHandler(handler)

    # Uvicorn installs its own handlers; let records propagate to ours instead.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # The request middleware writes one structured line per request, with the
    # request id and duration; uvicorn's access line would duplicate it.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    # HTTP client libraries log every provider call at INFO, full URL included.
    for name in ("httpx", "httpcore", "openai"):
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
