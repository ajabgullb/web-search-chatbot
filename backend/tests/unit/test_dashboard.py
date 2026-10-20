"""The committed Grafana dashboard stays in step with the metrics the API emits.

A panel that names a metric the API no longer exports does not error in
Grafana: it shows "No data", which reads as "nothing happened". This test
turns a rename in ``telemetry.py`` into a failing check instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY

import app.core.telemetry  # noqa: F401  (registers the metrics)

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD = REPO_ROOT / "observability/grafana/dashboards/web-search-chatbot.json"
DATASOURCES = REPO_ROOT / "observability/grafana/provisioning/datasources/datasources.yaml"

# Every selector on the dashboard is written with braces (``name{job="api"}``),
# so a name directly before ``{`` is a metric, never a function or a label.
SELECTOR = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{")
# Series Prometheus itself attaches to every scrape target.
PROMETHEUS_SERIES = {"up"}


def _panels() -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = json.loads(DASHBOARD.read_text())["panels"]
    return [panel for panel in panels if panel["type"] != "row"]


def _series_the_api_exports() -> set[str]:
    """Sample names as Prometheus stores them: counters gain ``_total``,
    histograms ``_bucket``/``_count``/``_sum``."""
    names: set[str] = set()
    for family in REGISTRY.collect():
        if family.type == "counter":
            names.add(f"{family.name}_total")
        elif family.type == "histogram":
            names.update(f"{family.name}{suffix}" for suffix in ("_bucket", "_count", "_sum"))
        else:
            names.add(family.name)
    return names


def _promql() -> list[tuple[str, str]]:
    return [
        (panel["title"], target["expr"])
        for panel in _panels()
        for target in panel["targets"]
        if target["datasource"]["uid"] == "prometheus"
    ]


def test_every_queried_metric_is_one_the_api_exports() -> None:
    exported = _series_the_api_exports() | PROMETHEUS_SERIES
    unknown = {
        (title, name)
        for title, expr in _promql()
        for name in SELECTOR.findall(expr)
        if name not in exported
    }
    assert not unknown, f"panels query metrics telemetry.py does not define: {sorted(unknown)}"


def test_every_query_is_scoped_to_the_api_job() -> None:
    """Prometheus may scrape more than this API one day; unscoped queries would
    silently start summing someone else's series."""
    unscoped = [
        title
        for title, expr in _promql()
        for selector in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*\{[^}]*\}", expr)
        if 'job="api"' not in selector
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


@pytest.mark.parametrize("panel", _panels(), ids=lambda panel: str(panel["title"]))
def test_every_panel_says_why_it_matters(panel: dict[str, Any]) -> None:
    """Shown as the panel's (i) tooltip: what the number means and when to worry."""
    assert panel["description"].strip()
