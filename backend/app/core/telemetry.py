"""Prometheus metrics.

Defined once at import time on the process-wide registry. Prometheus client
libraries require one definition per process, and every app instance in the
process (several, in tests) shares it. Tests therefore compare values before
and after an action rather than asserting absolute counts.

Label values come from small closed sets (route templates, outcome names,
error kinds), never from ids or user input. A label per conversation id would
create a new time series per conversation and exhaust the metrics backend.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy.pool import QueuePool

from app.db.session import Database

# Latency buckets in seconds. HTTP spans fast JSON calls to slow provider-bound
# streams; runs and first tokens are measured against a 90 s deadline.
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_RUN_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120)
_SEARCH_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20)
# Parsing is bounded by DOCUMENT_PARSE_TIMEOUT_SECONDS (30 s); embedding a
# whole document by its size and the provider's speed.
_DOCUMENT_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests by route template and status.",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "Time to response start by route template. For streams, time to first byte.",
    ["method", "route"],
    buckets=_HTTP_BUCKETS,
)
ACTIVE_STREAMS = Gauge("active_streams", "Run event streams currently open.")

RUNS = Counter("runs_total", "Finished runs by outcome.", ["outcome"])
RUN_DURATION = Histogram(
    "run_duration_seconds",
    "Run wall time, start to recorded outcome.",
    ["outcome"],
    buckets=_RUN_BUCKETS,
)
TIME_TO_FIRST_TOKEN = Histogram(
    "run_time_to_first_token_seconds",
    "From run start to the first answer text sent to the client.",
    buckets=_RUN_BUCKETS,
)
# Labelled by the configured model (a handful of names, not free text) so
# cost can be priced per model.
TOKENS = Counter("llm_tokens_total", "Model tokens reported by the provider.", ["kind", "model"])
# Failures by their stable error code (run_timeout, llm_unavailable, ...): the
# same closed set the client sees, so "why are runs failing?" has an answer.
RUN_FAILURES = Counter("run_failures_total", "Failed runs by error code.", ["code"])
# Completed answers by the evidence the agent went for: direct, web, documents, or
# documents_and_web. ``degraded`` marks an answer whose evidence could not be
# verified (a searched answer without one valid citation, or a search that
# failed). Quality regressions raise no errors; this is where they show.
ANSWERS = Counter(
    "answers_total",
    "Completed answers by evidence source and verification.",
    ["source", "degraded"],
)

SEARCHES = Counter("search_requests_total", "Search provider calls by outcome.", ["outcome"])
SEARCH_DURATION = Histogram(
    "search_request_duration_seconds", "Search provider call latency.", buckets=_SEARCH_BUCKETS
)
PROVIDER_FAILURES = Counter(
    "provider_failures_total", "Failed provider calls.", ["provider", "kind"]
)
RATE_LIMIT_HITS = Counter("rate_limit_hits_total", "Requests refused by a rate limit.", ["bucket"])

# Documents (ADR-0015). ``outcome`` on uploads is "accepted", "duplicate", or
# the stable error code the upload was refused with.
DOCUMENT_UPLOADS = Counter("document_uploads_total", "PDF uploads by outcome.", ["outcome"])
PDF_PARSE_DURATION = Histogram(
    "pdf_parse_duration_seconds",
    "Time to read a PDF in its child process, by outcome (ok or an error code).",
    ["outcome"],
    buckets=_DOCUMENT_BUCKETS,
)
DOCUMENTS_INGESTED = Counter(
    "documents_ingested_total", "Documents that finished ingestion.", ["outcome"]
)
DOCUMENT_EMBEDDING_DURATION = Histogram(
    "document_embedding_duration_seconds",
    "From a worker claiming a document to it being ready.",
    buckets=_DOCUMENT_BUCKETS,
)
EMBEDDING_REQUESTS = Counter(
    "embedding_requests_total",
    "Embedding provider calls by purpose (document or query) and outcome.",
    ["purpose", "outcome"],
)
EMBEDDING_DURATION = Histogram(
    "embedding_request_duration_seconds",
    "Embedding provider call latency.",
    ["purpose"],
    buckets=_SEARCH_BUCKETS,
)
EMBEDDING_TOKENS = Counter(
    "embedding_tokens_total", "Embedding tokens reported by the provider.", ["purpose"]
)
DOCUMENT_RETRIEVALS = Counter(
    "document_retrievals_total",
    "Searches of a conversation's documents: chunks (something relevant), "
    "empty (nothing above the floor), or failed.",
    ["outcome"],
)
DOCUMENT_RETRIEVAL_DURATION = Histogram(
    "document_retrieval_duration_seconds",
    "Embedding the question plus both searches.",
    buckets=_SEARCH_BUCKETS,
)

DB_POOL_CHECKED_OUT = Gauge("db_pool_connections_in_use", "Database connections checked out.")
DB_POOL_CAPACITY = Gauge(
    "db_pool_connections_max", "Most connections the pool will open (size + overflow)."
)
DB_POOL_SATURATION = Gauge(
    "db_pool_saturation_ratio", "Connections in use as a fraction of the maximum. 1.0 = full."
)

# Run outcomes as they appear on the wire, mapped to label values.
_RUN_OUTCOMES = {
    "message.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}


def record_http(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)


def record_run(
    event_type: str,
    seconds: float,
    *,
    error_code: str | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    outcome = _RUN_OUTCOMES.get(event_type, "failed")
    RUNS.labels(outcome).inc()
    RUN_DURATION.labels(outcome).observe(seconds)
    if outcome == "failed":
        RUN_FAILURES.labels(error_code or "internal_error").inc()
    if prompt_tokens:
        TOKENS.labels("prompt", model or "unknown").inc(prompt_tokens)
    if completion_tokens:
        TOKENS.labels("completion", model or "unknown").inc(completion_tokens)


def record_answer(*, searched: bool, read_documents: bool, degraded: bool) -> None:
    """Called for completed runs only; a failed run produced no answer."""
    if searched and read_documents:
        source = "documents_and_web"
    elif searched:
        source = "web"
    elif read_documents:
        source = "documents"
    else:
        source = "direct"
    ANSWERS.labels(source, str(degraded).lower()).inc()


def record_search(outcome: str, seconds: float) -> None:
    """``outcome`` is ok, transient_error, timeout, or permanent_error."""
    SEARCHES.labels(outcome).inc()
    SEARCH_DURATION.observe(seconds)
    if outcome != "ok":
        PROVIDER_FAILURES.labels("search", outcome).inc()


def record_llm_failure(kind: str) -> None:
    """``kind`` is transient or permanent."""
    PROVIDER_FAILURES.labels("llm", kind).inc()


def record_rate_limited(bucket: str) -> None:
    RATE_LIMIT_HITS.labels(bucket).inc()


def record_document_upload(outcome: str) -> None:
    DOCUMENT_UPLOADS.labels(outcome).inc()


def record_pdf_parse(outcome: str, seconds: float) -> None:
    PDF_PARSE_DURATION.labels(outcome).observe(seconds)


def record_document_ingested(outcome: str, seconds: float | None = None) -> None:
    """``outcome`` is ready or failed; the duration is recorded for ready only."""
    DOCUMENTS_INGESTED.labels(outcome).inc()
    if seconds is not None:
        DOCUMENT_EMBEDDING_DURATION.observe(seconds)


def record_embedding(purpose: str, outcome: str, seconds: float, tokens: int = 0) -> None:
    """``purpose`` is document or query; ``outcome`` ok, transient_error,
    permanent_error, or timeout."""
    EMBEDDING_REQUESTS.labels(purpose, outcome).inc()
    EMBEDDING_DURATION.labels(purpose).observe(seconds)
    if tokens:
        EMBEDDING_TOKENS.labels(purpose).inc(tokens)
    if outcome != "ok":
        PROVIDER_FAILURES.labels("embeddings", outcome).inc()


def record_document_retrieval(outcome: str, seconds: float) -> None:
    DOCUMENT_RETRIEVALS.labels(outcome).inc()
    DOCUMENT_RETRIEVAL_DURATION.observe(seconds)


def _sample_pool(database: Database) -> None:
    """Pool gauges are read at scrape time rather than on every checkout."""
    pool = database.engine.pool
    if not isinstance(pool, QueuePool):
        return
    capacity = pool.size() + pool._max_overflow  # no public accessor for the overflow cap
    in_use = pool.checkedout()
    DB_POOL_CHECKED_OUT.set(in_use)
    DB_POOL_CAPACITY.set(capacity)
    DB_POOL_SATURATION.set(in_use / capacity if capacity else 0.0)


def render(database: Database) -> tuple[bytes, str]:
    _sample_pool(database)
    return generate_latest(), CONTENT_TYPE_LATEST
