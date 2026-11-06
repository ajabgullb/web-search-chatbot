"""The MCP usage dashboard, the collector that feeds it, and what may leave it.

ADR-0018. Claude Code's telemetry carries tool inputs (SQL, URLs), error
messages, and account identifiers. The collector turns it into metrics, and
the only data that survives is the attributes its connector lists. These tests
pin that allowlist, so adding a label is a reviewed change rather than an
accident. They also keep the dashboard in step with the metric names the
collector actually produces, because a renamed metric shows "No data", which
looks the same as "nothing happened".
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD = REPO_ROOT / "observability/grafana/dashboards/mcp-usage.json"
COLLECTOR = REPO_ROOT / "observability/otel-collector/config.yaml"
DATASOURCES = REPO_ROOT / "observability/grafana/provisioning/datasources/datasources.yaml"
COMPOSE = REPO_ROOT / "compose.yaml"
MCP_JSON = REPO_ROOT / ".mcp.json"

# Every label an MCP metric may carry. Each comes from a small fixed set:
# server and tool names, true/false, an error class name, a five-character
# SQLSTATE, a connection status, accept/reject, a model id, a token type.
ALLOWED_ATTRIBUTES = {
    "mcp.server",
    "mcp.tool",
    "success",
    "error_type",
    "error.sqlstate",
    "status",
    "decision",
    "model",
    "type",
}
# Series the dashboard reads that the connector does not define: Prometheus's
# own, and the collector's count of records received.
EXTERNAL_SERIES = {"up", "otelcol_receiver_accepted_log_records"}
UNIT_SUFFIXES = {"ms": "_milliseconds"}

SELECTOR = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{")
SERVER_ALLOWLIST = re.compile(r'IsMatch\([a-z.]+\["mcp\.server"\], "\^\(([a-z|]+)\)\$"\)')


def _collector() -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(COLLECTOR.read_text())
    return config


def _metric_definitions() -> list[dict[str, Any]]:
    connector = _collector()["connectors"]["signal_to_metrics"]
    definitions: list[dict[str, Any]] = connector.get("logs", []) + connector.get("datapoints", [])
    return definitions


def _prometheus_names(definition: dict[str, Any]) -> set[str]:
    """The series names the collector's Prometheus exporter turns a definition into."""
    unit = definition.get("unit")
    if unit is not None:
        assert unit in UNIT_SUFFIXES, f"add the Prometheus suffix for unit {unit!r} to this test"
    base = definition["name"].replace(".", "_") + UNIT_SUFFIXES.get(unit or "", "")
    if "histogram" in definition:
        return {f"{base}_bucket", f"{base}_count", f"{base}_sum"}
    if definition.get("sum", {}).get("monotonic"):
        return {f"{base}_total"}
    return {base}


def _panels() -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = json.loads(DASHBOARD.read_text())["panels"]
    return [panel for panel in panels if panel["type"] != "row"]


def _promql() -> list[tuple[str, str]]:
    return [(panel["title"], target["expr"]) for panel in _panels() for target in panel["targets"]]


# --- What may leave the collector -------------------------------------------


@pytest.mark.parametrize("definition", _metric_definitions(), ids=lambda d: str(d["name"]))
def test_metrics_carry_only_allowlisted_attributes(definition: dict[str, Any]) -> None:
    keys = {attribute["key"] for attribute in definition.get("attributes", [])}
    assert keys <= ALLOWED_ATTRIBUTES, keys - ALLOWED_ATTRIBUTES
    # keys_expression would let incoming data pick its own labels.
    assert all("key" in attribute for attribute in definition.get("attributes", []))


@pytest.mark.parametrize("definition", _metric_definitions(), ids=lambda d: str(d["name"]))
def test_metrics_drop_resource_identity(definition: dict[str, Any]) -> None:
    """An empty include list means every resource attribute, host name and
    OS included, so each definition must name exactly the one it keeps."""
    assert definition["include_resource_attributes"] == [{"key": "service.name"}]


@pytest.mark.parametrize("definition", _metric_definitions(), ids=lambda d: str(d["name"]))
def test_metrics_count_only_this_projects_servers(definition: dict[str, Any]) -> None:
    """Claude Code reports MCP servers from every project. Only the three in
    .mcp.json are counted, which also bounds the series count."""
    servers = set(json.loads(MCP_JSON.read_text())["mcpServers"])
    condition = " ".join(definition["conditions"])
    allowlists = SERVER_ALLOWLIST.findall(condition)
    if allowlists:
        assert set(allowlists[0].split("|")) == servers, (
            "the collector's server allowlist must match .mcp.json"
        )
    else:
        # A metric for a single server names it outright.
        named = re.findall(r'\["mcp\.server"\] == "([a-z]+)"', condition)
        assert named, f"{definition['name']} counts servers from any project"
        assert set(named) <= servers


def test_sql_errors_are_reduced_to_their_sqlstate() -> None:
    """The error message can quote values from the query; only the code survives."""
    statements = json.dumps(_collector()["processors"]["transform/mcp_events"])
    assert "SQLSTATE (?P<code>[0-9A-Z]{5})" in statements
    for definition in _metric_definitions():
        keys = {attribute["key"] for attribute in definition.get("attributes", [])}
        assert "error" not in keys and "tool_input" not in keys and "tool_parameters" not in keys


def test_otlp_port_is_published_on_localhost_only() -> None:
    """Anyone who can reach the port can write metrics onto the dashboard."""
    ports = yaml.safe_load(COMPOSE.read_text())["services"]["otel-collector"]["ports"]
    assert ports and all(str(port).startswith("127.0.0.1:") for port in ports), ports


# --- The dashboard stays in step with the collector -------------------------


def test_every_queried_metric_is_one_the_collector_produces() -> None:
    produced = set().union(*(_prometheus_names(d) for d in _metric_definitions()))
    produced |= EXTERNAL_SERIES
    unknown = {
        (title, name)
        for title, expr in _promql()
        for name in SELECTOR.findall(expr)
        if name not in produced
    }
    assert not unknown, f"panels query metrics the collector does not produce: {sorted(unknown)}"


def test_every_query_is_scoped_to_the_mcp_job() -> None:
    unscoped = [
        title
        for title, expr in _promql()
        for selector in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*\{[^}]*\}", expr)
        if 'job="mcp"' not in selector
    ]
    assert not unscoped, unscoped


def test_panels_use_only_provisioned_datasources() -> None:
    provisioned = set(re.findall(r"^\s+uid: (\S+)$", DATASOURCES.read_text(), re.MULTILINE))
    used = {target["datasource"]["uid"] for panel in _panels() for target in panel["targets"]}
    assert used <= provisioned, used - provisioned


def test_panels_have_unique_ids_and_fit_the_grid() -> None:
    panels: list[dict[str, Any]] = json.loads(DASHBOARD.read_text())["panels"]
    ids = [panel["id"] for panel in panels]
    assert len(ids) == len(set(ids))
    for panel in panels:
        grid = panel["gridPos"]
        assert grid["x"] + grid["w"] <= 24, panel["title"]


def test_dashboard_uid_differs_from_the_main_dashboard() -> None:
    main = json.loads((DASHBOARD.parent / "web-search-chatbot.json").read_text())
    assert json.loads(DASHBOARD.read_text())["uid"] != main["uid"]


@pytest.mark.parametrize("panel", _panels(), ids=lambda panel: str(panel["title"]))
def test_every_panel_says_why_it_matters(panel: dict[str, Any]) -> None:
    assert panel["description"].strip()
