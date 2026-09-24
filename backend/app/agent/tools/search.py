"""The search step: validated queries in, normalized citable sources out.

Everything the rest of the system relies on about a source is decided here —
that its URL is safe to render, that the same page fetched twice is one source,
and that its label is stable — so a change of provider cannot change any of it.

Budgets are enforced in this module rather than by the graph's shape. The graph
has no cycle: ``web_search`` runs once and executes at most
``max_searches_per_run`` queries inside a hard counter. A bounded loop with no
back edge cannot run away.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.agent.context import AgentLimits
from app.agent.events import AgentEvent, SearchCompleted, SearchStarted
from app.agent.state import Source
from app.providers.search import (
    SearchHit,
    SearchPermanentError,
    SearchProvider,
    SearchTransientError,
)

logger = logging.getLogger(__name__)

# Anything else — javascript:, data:, file: — is a rendering hazard, not a
# source. AGENTS.md §6 permits http and https only.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Dropped before comparing two URLs, so the same article shared through three
# campaigns is one source. Parameters whose meaning is site-specific (``ref``,
# ``id``) are left alone: guessing wrong there would merge two distinct pages.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}

MAX_QUERY_CHARS = 400
MAX_SNIPPET_CHARS = 1_200
DISPLAY_URL_MAX_CHARS = 80

# One retry, after a short pause, for transient failures only. Long enough to
# clear a blip, short enough that it cannot meaningfully eat the run deadline.
RETRY_BACKOFF_SECONDS = 0.5


def canonicalize_url(raw: str) -> str | None:
    """Return a canonical absolute URL, or ``None`` if it cannot be trusted.

    Rejecting is the safe default: a source we cannot canonicalize is a source
    we cannot dedupe or safely render, and dropping it costs one result.
    """
    if not raw:
        return None
    candidate = raw.strip()
    # A control character in a URL is either corruption or an attempt to break
    # out of whatever renders it.
    if any(ch.isspace() or ord(ch) < 0x20 for ch in candidate):
        return None

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES or not parts.hostname:
        return None

    host = parts.hostname.lower()
    port = parts.port
    netloc = host
    if port is not None and str(port) != _DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    # Sorted so that two orderings of the same parameters compare equal.
    query = urlencode(sorted(kept))

    # The fragment never identifies a different document to a search engine.
    return urlunsplit((scheme, netloc, path, query, ""))


def dedupe_key(canonical_url: str) -> str:
    """Identity of a page for deduplication.

    Scheme and a leading ``www.`` are ignored: http/https and www/non-www are
    overwhelmingly the same document, and showing it twice is a worse error
    than merging two rare edge cases.
    """
    parts = urlsplit(canonical_url)
    host = (parts.hostname or "").removeprefix("www.")
    tail = urlunsplit(("", "", parts.path, parts.query, ""))
    return f"{host}{tail}"


def build_display_url(canonical_url: str) -> str:
    """A short, human-readable form for a link label."""
    parts = urlsplit(canonical_url)
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path if parts.path != "/" else ""
    shown = f"{host}{path}"
    if len(shown) <= DISPLAY_URL_MAX_CHARS:
        return shown
    return shown[: DISPLAY_URL_MAX_CHARS - 1] + "…"


def validate_query(raw: str) -> str | None:
    """Clean one query, or reject it.

    The model proposes queries, so this is a trust boundary: a query is sent to
    a third party, and its shape is never taken on faith.
    """
    collapsed = " ".join(raw.split())
    if not collapsed:
        return None
    return collapsed[:MAX_QUERY_CHARS]


def validate_queries(raw_queries: list[str], *, limit: int) -> list[str]:
    """Clean, deduplicate case-insensitively, and cap at the run's budget."""
    seen: set[str] = set()
    queries: list[str] = []
    for raw in raw_queries:
        query = validate_query(raw)
        if query is None:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) >= limit:
            break
    return queries


@dataclass(slots=True)
class SearchOutcome:
    """What the search step produced, including how it failed."""

    sources: list[Source] = field(default_factory=list)
    executed_queries: list[str] = field(default_factory=list)
    # Set when every attempted query failed at the provider. Distinct from
    # "searched and found nothing", which is a legitimate empty result.
    provider_failed: bool = False
    failure_reason: str = ""

    @property
    def searches_run(self) -> int:
        return len(self.executed_queries)


def normalize_hits(
    hits: list[SearchHit],
    *,
    seen_keys: set[str],
    next_ordinal: int,
    retrieved_at: datetime,
    limit: int,
) -> list[Source]:
    """Turn provider hits into citable sources, skipping unusable ones."""
    sources: list[Source] = []
    for hit in hits:
        if len(sources) >= limit:
            break
        canonical = canonicalize_url(hit.url)
        if canonical is None:
            continue
        key = dedupe_key(canonical)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ordinal = next_ordinal + len(sources)
        sources.append(
            Source(
                source_id=f"S{ordinal}",
                url=canonical,
                display_url=build_display_url(canonical),
                title=hit.title.strip() or build_display_url(canonical),
                snippet=hit.snippet.strip()[:MAX_SNIPPET_CHARS],
                publisher=hit.publisher,
                published_at=hit.published_at,
                retrieved_at=retrieved_at,
            )
        )
    return sources


async def execute_searches(
    queries: list[str],
    *,
    provider: SearchProvider,
    limits: AgentLimits,
    emit: Callable[[AgentEvent], None] | None = None,
) -> SearchOutcome:
    """Run the validated queries and return normalized, deduplicated sources.

    Retries exactly one transient failure per query. A permanent failure — bad
    credentials, a rejected query, exhausted quota — is never retried: the
    second call fails identically and costs the same.

    ``emit`` reports each search as it starts and finishes. It is a plain
    callback so this module stays ignorant of how progress reaches a user.
    """
    report = emit or (lambda _event: None)
    outcome = SearchOutcome()
    budget = validate_queries(queries, limit=limits.max_searches_per_run)
    if not budget:
        return outcome

    seen_keys: set[str] = set()
    failures = 0

    for query in budget:
        # The hard counter. It cannot be exceeded even if a caller hands us a
        # longer list than the budget allows.
        if outcome.searches_run >= limits.max_searches_per_run:
            break

        report(SearchStarted(query=query))
        hits: list[SearchHit] | None = None
        for attempt in (1, 2):
            try:
                async with asyncio.timeout(limits.search_timeout_seconds):
                    hits = await provider.search(query, max_results=limits.max_results_per_search)
                break
            except (SearchTransientError, TimeoutError) as exc:
                if attempt == 2:
                    logger.warning(
                        "search_failed",
                        extra={
                            "reason": "transient",
                            "attempts": 2,
                            "error_type": type(exc).__name__,
                        },
                    )
                    break
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
            except SearchPermanentError as exc:
                logger.warning(
                    "search_failed",
                    extra={
                        "reason": "permanent",
                        "attempts": attempt,
                        "error_type": type(exc).__name__,
                    },
                )
                break

        outcome.executed_queries.append(query)
        if hits is None:
            failures += 1
            report(SearchCompleted(query=query, sources=(), failed=True))
            continue

        new_sources = normalize_hits(
            hits,
            seen_keys=seen_keys,
            next_ordinal=len(outcome.sources) + 1,
            retrieved_at=datetime.now(UTC),
            limit=limits.max_results_per_search,
        )
        outcome.sources.extend(new_sources)
        report(SearchCompleted(query=query, sources=tuple(new_sources)))

    if failures and failures == len(outcome.executed_queries):
        outcome.provider_failed = True
        outcome.failure_reason = "every search request failed"
    return outcome
