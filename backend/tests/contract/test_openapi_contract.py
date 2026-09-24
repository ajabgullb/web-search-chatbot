"""The public API contract: the committed schema, and the rules it must keep.

``openapi.json`` is what the frontend client is generated from. CI diffs it,
but these tests fail in a plain ``make test`` too, so drift is caught before
a push rather than after.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from app.core.config import Environment, Settings
from app.main import create_app
from app.schemas.runs import RunEventModel

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMITTED = REPO_ROOT / "openapi.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    # Development settings: production hides the schema altogether.
    settings = Settings(environment=Environment.development, redis_url="")
    return create_app(settings).openapi()


def operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def test_the_committed_schema_matches_the_code(schema: dict[str, Any]) -> None:
    """If this fails: run `make generate-client` and commit both files."""
    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(schema, sort_keys=True))


def test_every_endpoint_is_versioned(schema: dict[str, Any]) -> None:
    assert all(path.startswith("/api/v1/") for path in schema["paths"])


def test_every_operation_documents_the_error_envelope(schema: dict[str, Any]) -> None:
    for path, method, operation in operations(schema):
        default = operation["responses"].get("default", {})
        ref = default.get("content", {}).get("application/json", {}).get("schema", {})
        assert ref.get("$ref") == "#/components/schemas/ErrorResponse", f"{method} {path}"


def test_operation_ids_are_unique(schema: dict[str, Any]) -> None:
    ids = [operation["operationId"] for _, _, operation in operations(schema)]
    assert len(ids) == len(set(ids))


def test_the_stream_publishes_every_event_type(schema: dict[str, Any]) -> None:
    """The run endpoint's text/event-stream schema is the client's only source
    of event types. An event model missing from it would never reach the
    generated client."""
    stream = schema["paths"]["/api/v1/conversations/{conversation_id}/runs"]["post"]
    union = stream["responses"]["200"]["content"]["text/event-stream"]["schema"]
    published = set(union["discriminator"]["mapping"])
    models = {model.model_fields["type"].default for model in get_args(RunEventModel)}
    assert published == models
    assert published == {
        "run.started",
        "agent.status",
        "search.started",
        "search.completed",
        "message.delta",
        "citation",
        "message.completed",
        "run.failed",
        "run.cancelled",
    }


def test_operator_endpoints_stay_out_of_the_public_contract(schema: dict[str, Any]) -> None:
    assert "/api/v1/metrics" not in schema["paths"]
