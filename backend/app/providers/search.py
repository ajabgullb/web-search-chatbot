"""Web search, behind an interface.

One implementation ships: Tavily over its REST API. It is called through a
shared ``httpx`` client built once in the FastAPI lifespan, never per request.

The provider's job stops at "return hits". Canonicalisation, deduplication,
source numbering, and every budget live in ``app/agent/tools/search.py``, so
swapping the provider cannot change how sources are identified or cited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result, in our vocabulary rather than the provider's."""

    url: str
    title: str
    snippet: str
    publisher: str | None = None
    published_at: datetime | None = None


class SearchError(Exception):
    """Base for every failure this module raises."""


class SearchTransientError(SearchError):
    """Timeout, connection reset, or a 5xx/429 — one bounded retry is sane."""


class SearchPermanentError(SearchError):
    """Bad credentials, a rejected query, or exhausted quota."""


class SearchProvider(Protocol):
    async def search(self, query: str, *, max_results: int) -> list[SearchHit]: ...

    async def aclose(self) -> None: ...


_TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class TavilySearchProvider:
    """Tavily's ``/search`` endpoint.

    Response parsing is deliberately forgiving: a provider that adds or renames
    a peripheral field should cost us a missing publisher, not a failed run. A
    hit missing a URL or a title is dropped, because neither can be cited.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = settings.search_api_key
        self._endpoint = settings.search_base_url.rstrip("/") + "/search"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.search_timeout_seconds),
            # A search burst is a handful of calls; a small pool is plenty and
            # keeps one slow provider from eating every available socket.
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        if not self._api_key:
            raise SearchPermanentError("SEARCH_API_KEY is not configured")

        try:
            response = await self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    # We synthesise the answer ourselves from cited sources;
                    # the provider's own answer would have no traceable source.
                    "include_answer": False,
                    # Page bodies are untrusted, large, and never shown to the
                    # user. Snippets are enough to ground a cited claim.
                    "include_raw_content": False,
                },
            )
        except httpx.TimeoutException as exc:
            raise SearchTransientError("search provider timed out") from exc
        except httpx.HTTPError as exc:
            raise SearchTransientError("search provider unreachable") from exc

        if response.status_code in _TRANSIENT_STATUSES:
            raise SearchTransientError(f"search provider returned {response.status_code}")
        if response.status_code >= 400:
            # The provider's error body may quote the query or the key; it never
            # reaches a log or a client.
            raise SearchPermanentError(f"search provider returned {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchTransientError("search provider returned malformed JSON") from exc

        return _parse_hits(payload)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_hits(payload: Any) -> list[SearchHit]:
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []

    hits: list[SearchHit] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        title = entry.get("title")
        if not isinstance(url, str) or not isinstance(title, str) or not url or not title:
            continue
        snippet = entry.get("content")
        hits.append(
            SearchHit(
                url=url,
                title=title.strip(),
                snippet=snippet.strip() if isinstance(snippet, str) else "",
                publisher=None,
                published_at=_parse_timestamp(entry.get("published_date")),
            )
        )
    return hits


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a publication date, tolerating the several shapes providers use."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A naive stamp is assumed UTC: the alternative is a comparison against an
    # aware "now" that raises at runtime.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_search_provider(settings: Settings) -> SearchProvider:
    if settings.search_provider != "tavily":
        raise ValueError(
            f"Unsupported SEARCH_PROVIDER {settings.search_provider!r}; only 'tavily' exists"
        )
    return TavilySearchProvider(settings)
