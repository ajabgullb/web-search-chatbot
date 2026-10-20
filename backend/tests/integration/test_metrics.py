"""Prometheus metrics: what a run records, and who may scrape them.

Metrics live on the process-wide registry shared by every app a test builds,
so each test measures the change its own actions caused.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from prometheus_client import REGISTRY

from app.agent.routing import SearchDecision
from app.core.config import Environment
from app.providers.embeddings import EmbeddingAuthError
from app.providers.llm import LLMPermanentError
from app.providers.search import SearchPermanentError
from tests.fakes import FakeEmbeddings, FakeLLM, FakeSearchProvider, hit
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import install_providers, parse_sse, post_run
from tests.pdfs import make_pdf

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


# --- documents (ADR-0015) -------------------------------------------------------


async def _document_user(app: FastAPI, client: AsyncClient) -> tuple[dict[str, str], str]:
    await app.state.embedding_provider.aclose()
    app.state.embedding_provider = FakeEmbeddings()
    # The fake embedder's similarities sit far below a real model's.
    app.state.agent_limits = replace(app.state.agent_limits, document_min_similarity=0.05)
    headers = auth_header(await register(client, email=f"docs-{id(client)}@example.com"))
    conversation_id = str((await create_conversation(client, headers))["id"])
    return headers, conversation_id


async def _upload_until_settled(
    client: AsyncClient, headers: dict[str, str], conversation_id: str, data: bytes
) -> dict[str, object]:
    uploaded = await client.post(
        f"/api/v1/conversations/{conversation_id}/documents",
        files={"file": ("report.pdf", data, "application/pdf")},
        headers=headers,
    )
    assert uploaded.status_code == 202, uploaded.text
    async with asyncio.timeout(15):
        while True:
            body = (
                await client.get(
                    f"/api/v1/conversations/{conversation_id}/documents/{uploaded.json()['id']}",
                    headers=headers,
                )
            ).json()
            if body["status"] != "processing":
                return dict(body)
            await asyncio.sleep(0.05)


async def test_a_document_is_counted_from_upload_to_a_cited_answer(
    app: FastAPI, client: AsyncClient
) -> None:
    headers, conversation_id = await _document_user(app, client)
    accepted = delta("document_uploads_total", outcome="accepted")
    parsed = delta("pdf_parse_duration_seconds_count", outcome="ok")
    ready = delta("documents_ingested_total", outcome="ready")
    embedded = delta("document_embedding_duration_seconds_count")
    document_calls = delta("embedding_requests_total", purpose="document", outcome="ok")
    document_tokens = delta("embedding_tokens_total", purpose="document")

    settled = await _upload_until_settled(
        client, headers, conversation_id, make_pdf(["Quarterly revenue grew twelve percent."])
    )

    assert settled["status"] == "ready"
    assert (accepted(), parsed(), ready(), embedded()) == (1, 1, 1, 1)
    assert document_calls() == 1
    assert document_tokens() > 0

    query_calls = delta("embedding_requests_total", purpose="query", outcome="ok")
    found = delta("document_retrievals_total", outcome="chunks")
    timed = delta("document_retrieval_duration_seconds_count")
    await install_providers(app, llm=FakeLLM(answers=["It grew [D1]."]))
    run = parse_sse(
        (
            await post_run(
                client, headers, conversation_id, content="What does the report say about revenue?"
            )
        ).text
    )

    assert run.terminal["type"] == "message.completed"
    assert (query_calls(), found(), timed()) == (1, 1, 1)


async def test_refused_and_repeated_uploads_are_counted_by_outcome(
    app: FastAPI, client: AsyncClient
) -> None:
    headers, conversation_id = await _document_user(app, client)
    invalid = delta("document_uploads_total", outcome="pdf_invalid")
    no_text = delta("document_uploads_total", outcome="pdf_no_text")
    duplicate = delta("document_uploads_total", outcome="duplicate")
    url = f"/api/v1/conversations/{conversation_id}/documents"

    await client.post(
        url, files={"file": ("a.pdf", b"not a pdf", "application/pdf")}, headers=headers
    )
    await client.post(
        url, files={"file": ("b.pdf", make_pdf([""]), "application/pdf")}, headers=headers
    )
    await _upload_until_settled(client, headers, conversation_id, make_pdf(["once"]))
    await client.post(
        url, files={"file": ("c.pdf", make_pdf(["once"]), "application/pdf")}, headers=headers
    )

    assert (invalid(), no_text(), duplicate()) == (1, 1, 1)


async def test_an_embedding_outage_shows_as_a_provider_failure(
    app: FastAPI, client: AsyncClient
) -> None:
    headers, conversation_id = await _document_user(app, client)
    app.state.embedding_provider = FakeEmbeddings(error=EmbeddingAuthError("bad key"))
    failures = delta("provider_failures_total", provider="embeddings", kind="permanent_error")
    failed_documents = delta("documents_ingested_total", outcome="failed")
    failed_calls = delta("embedding_requests_total", purpose="document", outcome="permanent_error")

    settled = await _upload_until_settled(client, headers, conversation_id, make_pdf(["x y z"]))

    assert settled["status"] == "failed"
    assert (failures(), failed_documents(), failed_calls()) == (1, 1, 1)


async def test_document_metrics_are_exposed_for_scraping(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/metrics")).text
    for name in (
        "document_uploads_total",
        "pdf_parse_duration_seconds",
        "documents_ingested_total",
        "embedding_requests_total",
        "embedding_tokens_total",
        "document_retrievals_total",
    ):
        assert f"# TYPE {name}" in body, name
