"""Structured logging: fields, correlation, and the redaction safety net."""

from __future__ import annotations

import json
import logging
import sys
import uuid

import pytest

from app.core.logging import (
    ConsoleFormatter,
    JsonFormatter,
    bind_log_context,
    clear_log_context,
    redact,
    user_ref,
)
from app.core.request_context import set_request_id

FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl"


def record(message: str, *, exc: BaseException | None = None, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("app.test", logging.INFO, __file__, 1, message, None, None)
    if exc is not None:
        try:
            raise exc
        except BaseException:
            rec.exc_info = sys.exc_info()
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def as_json(rec: logging.LogRecord) -> dict[str, object]:
    return dict(json.loads(JsonFormatter().format(rec)))


@pytest.fixture(autouse=True)
def fresh_context() -> None:
    clear_log_context()
    set_request_id("")


def test_a_line_is_one_json_object_with_the_event_and_its_fields() -> None:
    line = as_json(record("run_finished", status="message.completed", duration_ms=812))

    assert line["event"] == "run_finished"
    assert line["level"] == "INFO"
    assert line["status"] == "message.completed"
    assert line["duration_ms"] == 812
    assert str(line["timestamp"]).endswith("+00:00")


def test_request_and_bound_context_appear_on_every_line() -> None:
    set_request_id("req-123")
    run_id = uuid.uuid4()
    bind_log_context(user="abc", run_id=str(run_id))

    line = as_json(record("anything"))

    assert line["request_id"] == "req-123"
    assert line["user"] == "abc"
    assert line["run_id"] == str(run_id)


def test_fields_named_like_secrets_or_content_are_masked() -> None:
    line = as_json(
        record(
            "oops",
            password="hunter2hunter2",
            refresh_token="opaque-refresh-value",
            content="the user's private question",
            snippet="page text",
            query="what the user searched",
        )
    )

    for key in ("password", "refresh_token", "content", "snippet", "query"):
        assert line[key] == "[REDACTED]"
    text = json.dumps(line)
    assert "hunter2" not in text
    assert "private question" not in text


@pytest.mark.parametrize(
    ("raw", "leak"),
    [
        (f"Authorization: Bearer {FAKE_JWT}", FAKE_JWT),
        (f"token {FAKE_JWT} seen", FAKE_JWT),
        ("password=hunter2hunter2 in a driver message", "hunter2hunter2"),
        ("api_key: 'sk-proj-abcdefghijklmnop'", "sk-proj-abcdefghijklmnop"),
        ("tavily key tvly-abcdefghijklmnop", "tvly-abcdefghijklmnop"),
        ("Key (email)=(alice@example.com) already exists", "alice@example.com"),
    ],
)
def test_credential_shaped_text_is_scrubbed(raw: str, leak: str) -> None:
    assert leak not in redact(raw)


def test_tracebacks_are_scrubbed_too() -> None:
    line = as_json(record("run_crashed", exc=ValueError(f"bad token {FAKE_JWT}")))

    assert line["error_type"] == "ValueError"
    assert FAKE_JWT not in str(line["exception"])


def test_the_console_format_applies_the_same_redaction() -> None:
    bind_log_context(user="abc")
    text = ConsoleFormatter().format(
        record(f"Bearer {FAKE_JWT}", password="hunter2hunter2", status=200)
    )

    assert FAKE_JWT not in text
    assert "hunter2" not in text
    assert "user=abc" in text
    assert "status=200" in text


def test_user_ref_is_stable_and_is_not_the_id() -> None:
    user_id = uuid.uuid4()

    assert user_ref(user_id) == user_ref(user_id)
    assert user_ref(user_id) != user_ref(uuid.uuid4())
    assert str(user_id) not in user_ref(user_id)
    assert user_id.hex not in user_ref(user_id)
