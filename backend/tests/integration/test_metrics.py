"""Prometheus metrics: what a run records, and who may scrape them.

Metrics live on the process-wide registry shared by every app a test builds,
so each test measures the change its own actions caused.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from prometheus_client import REGISTRY

from app.agent.routing import SearchDecision
from app.core.config import Environment
from app.providers.llm import LLMPermanentError
from app.providers.search import SearchPermanentError
from tests.fakes import FakeLLM, FakeSearchProvider, hit
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import install_providers, parse_sse, post_run

pytestmark = pytest.mark.integration

SEARCHING = SearchDecision(should_search=True, rationale="fresh", queries=["rates"])


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def route_samples(method: str, template: str, status: str) -> float:
    """HTTP requests for one route template. Matched by suffix: whether the
    template includes the /api/v1 prefix depends on how FastAPI nests routers."""
    return sum(
        metric_sample.value
        for metric in REGISTRY.collect()
        if metric.name == "http_requests"
        for metric_sample in metric.samples
        if metric_sample.name == "http_requests_total"
        and metric_sample.labels["method"] == method
        and metric_sample.labels["status"] == status
        and metric_sample.labels["route"].endswith(template)
        and "{" in metric_sample.labels["route"]
    )


def route_delta(method: str, template: str, status: str) -> Callable[[], float]:
    before = route_samples(method, template, status)
    return lambda: route_samples(method, template, status) - before


def delta(name: str, **labels: str) -> Callable[[], float]:
    """Read a metric now; the returned function gives the change since."""
    before = sample(name, **labels)
    return lambda: sample(name, **labels) - before


async def test_a_searched_run_records_its_outcome_latency_and_search(
    app: FastAPI, client: AsyncClient
) -> None:
    await install_providers(
        app,
        llm=FakeLLM(answers=["Rates held [S1]."], decision=SEARCHING),
        search=FakeSearchProvider(default=[hit("https://news.test/a", snippet="held")]),
    )
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    completed = delta("runs_total", outcome="completed")
    durations = delta("run_duration_seconds_count", outcome="completed")
    first_tokens = delta("run_time_to_first_token_seconds_count")
    searches = delta("search_requests_total", outcome="ok")
    search_latency = delta("search_request_duration_seconds_count")
    runs_route = route_delta("POST", "/conversations/{conversation_id}/runs", "200")
    streams_before = sample("active_streams")

    response = await post_run(client, headers, conversation["id"], content="rates today?")

    assert parse_sse(response.text).terminal["type"] == "message.completed"
    assert completed() == 1
    assert durations() == 1
    assert first_tokens() == 1
    assert searches() == 1
    assert search_latency() == 1
    # Labelled by route template, so a million conversations are one series.
    assert runs_route() == 1
    assert sample("active_streams") == streams_before


async def test_provider_failures_are_counted(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(
        app,
        llm=FakeLLM(decision=SEARCHING, answer_error=LLMPermanentError("down")),
        search=FakeSearchProvider(error=SearchPermanentError("bad key")),
    )
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    search_failures = delta("provider_failures_total", provider="search", kind="permanent_error")
    llm_failures = delta("provider_failures_total", provider="llm", kind="permanent")
    failed_runs = delta("runs_total", outcome="failed")

    response = await post_run(client, headers, conversation["id"], content="rates today?")

    assert parse_sse(response.text).terminal["type"] == "run.failed"
    assert search_failures() == 1
    assert llm_failures() == 1
    assert failed_runs() == 1


async def test_rate_limit_refusals_are_counted(app: FastAPI, client: AsyncClient) -> None:
    await install_providers(app, llm=FakeLLM(answers=["a", "b"]))
    app.state.settings.rate_limit_runs = 1
    headers = auth_header(await register(client))
    conversation = await create_conversation(client, headers)
    refused = delta("rate_limit_hits_total", bucket="runs")

    await post_run(client, headers, conversation["id"], content="a")
    limited = await post_run(client, headers, conversation["id"], content="b")

    assert limited.status_code == 429
    assert refused() == 1


async def test_the_scrape_exposes_the_required_series(client: AsyncClient) -> None:
    response = await client.get("/api/v1/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for series in (
        "http_requests_total",
        "run_duration_seconds",
        "run_time_to_first_token_seconds",
        "search_request_duration_seconds",
        "rate_limit_hits_total",
        "provider_failures_total",
        "active_streams",
        "db_pool_connections_in_use",
        "db_pool_saturation_ratio",
    ):
        assert series in response.text, series


class TestScrapeAccess:
    async def test_a_configured_token_is_required(self, app: FastAPI, client: AsyncClient) -> None:
        app.state.settings.metrics_token = "m" * 40

        missing = await client.get("/api/v1/metrics")
        wrong = await client.get("/api/v1/metrics", headers={"Authorization": "Bearer " + "x" * 40})
        right = await client.get("/api/v1/metrics", headers={"Authorization": "Bearer " + "m" * 40})

        assert missing.status_code == wrong.status_code == 401
        assert right.status_code == 200

    async def test_production_without_a_token_does_not_serve_metrics(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        app.state.settings.environment = Environment.production

        response = await client.get("/api/v1/metrics")

        assert response.status_code == 404
        assert "http_requests_total" not in response.text

    async def test_metrics_are_not_part_of_the_public_schema(self, client: AsyncClient) -> None:
        schema = (await client.get("/api/v1/openapi.json")).json()
        assert "/api/v1/metrics" not in schema["paths"]
