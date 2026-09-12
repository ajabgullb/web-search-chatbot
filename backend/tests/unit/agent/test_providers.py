"""Provider-boundary behaviour: error classification and response parsing.

No test here reaches a network. The point is that a provider's failure is
translated into our own retry decision before it escapes the module.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.providers.llm import (
    LLMPermanentError,
    LLMTransientError,
    build_llm_provider,
    classify_llm_error,
)
from app.providers.search import (
    SearchPermanentError,
    SearchTransientError,
    TavilySearchProvider,
    build_search_provider,
)


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class TestLLMErrorClassification:
    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_retryable_statuses(self, status: int) -> None:
        assert isinstance(classify_llm_error(_StatusError(status)), LLMTransientError)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_non_retryable_statuses(self, status: int) -> None:
        """Retrying a bad key or a rejected request only pays for it twice."""
        assert isinstance(classify_llm_error(_StatusError(status)), LLMPermanentError)

    def test_a_timeout_is_transient(self) -> None:
        assert isinstance(classify_llm_error(TimeoutError()), LLMTransientError)

    def test_an_unrecognised_failure_defaults_to_permanent(self) -> None:
        """An error we do not understand is not one to retry blindly."""
        assert isinstance(classify_llm_error(ValueError("odd")), LLMPermanentError)


def settings_with(**overrides: object) -> Settings:
    values: dict[str, object] = {"search_api_key": "test-key"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class TestSearchProvider:
    async def test_a_missing_key_fails_permanently_without_a_request(self) -> None:
        provider = TavilySearchProvider(settings_with(search_api_key=""))
        with pytest.raises(SearchPermanentError):
            await provider.search("anything", max_results=3)
        await provider.aclose()

    async def test_results_are_mapped_into_our_own_type(self) -> None:
        payload = {
            "results": [
                {
                    "url": "https://a.test/1",
                    "title": "  A title  ",
                    "content": " some evidence ",
                    "published_date": "2026-01-02T03:04:05Z",
                    "score": 0.9,
                },
                {"title": "no url"},
                {"url": "https://b.test/2", "title": "Second"},
                "not an object",
            ]
        }
        provider = TavilySearchProvider(
            settings_with(), client=_client_returning(httpx.Response(200, json=payload))
        )
        hits = await provider.search("q", max_results=5)

        assert [h.url for h in hits] == ["https://a.test/1", "https://b.test/2"]
        assert hits[0].title == "A title"
        assert hits[0].snippet == "some evidence"
        assert hits[0].published_at is not None
        assert hits[0].published_at.tzinfo is not None
        assert hits[1].snippet == ""

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_transient_statuses_are_retryable(self, status: int) -> None:
        provider = TavilySearchProvider(
            settings_with(), client=_client_returning(httpx.Response(status))
        )
        with pytest.raises(SearchTransientError):
            await provider.search("q", max_results=3)

    @pytest.mark.parametrize("status", [400, 401, 403, 422])
    async def test_client_errors_are_permanent(self, status: int) -> None:
        provider = TavilySearchProvider(
            settings_with(), client=_client_returning(httpx.Response(status, text="secret detail"))
        )
        with pytest.raises(SearchPermanentError) as caught:
            await provider.search("q", max_results=3)
        # The provider's body may quote the key or the query; it must not travel.
        assert "secret detail" not in str(caught.value)

    async def test_a_timeout_is_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        provider = TavilySearchProvider(
            settings_with(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        with pytest.raises(SearchTransientError):
            await provider.search("q", max_results=3)

    async def test_malformed_json_does_not_escape_as_a_parse_error(self) -> None:
        provider = TavilySearchProvider(
            settings_with(), client=_client_returning(httpx.Response(200, text="<html>"))
        )
        with pytest.raises(SearchTransientError):
            await provider.search("q", max_results=3)


class TestFactories:
    def test_an_unknown_llm_provider_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="openai_compatible"):
            build_llm_provider(settings_with(llm_provider="magic"))

    def test_an_unknown_search_provider_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tavily"):
            build_search_provider(settings_with(search_provider="magic"))


def _client_returning(response: httpx.Response) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response))
