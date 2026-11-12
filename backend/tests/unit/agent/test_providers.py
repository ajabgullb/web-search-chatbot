"""Provider-boundary behaviour: error classification and response parsing.

No test here reaches a network. The point is that a provider's failure is
translated into our own retry decision before it escapes the module.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.providers.llm import (
    LangChainLLM,
    LLMAuthError,
    LLMKeyRejected,
    LLMModelUnavailable,
    LLMPermanentError,
    LLMTransientError,
    OpenAICompatibleLLM,
    build_llm_provider,
    build_user_llm,
    classify_llm_error,
)
from app.providers.llm_catalog import PRESETS
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

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_credentials_are_their_own_kind(self, status: int) -> None:
        """A bad key must be told apart from an outage, yet still never retried."""
        error = classify_llm_error(_StatusError(status))
        assert isinstance(error, LLMAuthError)
        assert isinstance(error, LLMPermanentError)

    @pytest.mark.parametrize("status", [400, 404, 422])
    def test_other_rejections_are_not_auth_failures(self, status: int) -> None:
        assert not isinstance(classify_llm_error(_StatusError(status)), LLMAuthError)

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

    def test_no_server_key_means_no_server_model(self) -> None:
        """ADR-0020: users bring their own keys instead."""
        assert build_llm_provider(settings_with(llm_api_key="")) is None
        assert build_llm_provider(settings_with(llm_api_key="sk-test")) is not None

    def test_an_unknown_search_provider_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tavily"):
            build_search_provider(settings_with(search_provider="magic"))


def _client_returning(response: httpx.Response) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response))


class TestLLMStreaming:
    """The real provider class, with LangChain's own fake chat model standing in
    for the network. Proves chunk mapping and error classification without a
    request."""

    @staticmethod
    def _provider_with(model: object) -> OpenAICompatibleLLM:
        provider = OpenAICompatibleLLM(settings_with(llm_api_key="test"))
        provider._client = model
        return provider

    async def test_text_arrives_in_chunks(self) -> None:
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage, HumanMessage

        model = GenericFakeChatModel(messages=iter([AIMessage(content="Hello there, friend.")]))
        provider = self._provider_with(model)

        chunks = [c async for c in provider.stream([HumanMessage(content="hi")])]

        assert len(chunks) > 1
        assert "".join(c.text for c in chunks) == "Hello there, friend."

    async def test_a_failure_mid_stream_is_classified(self) -> None:
        from langchain_core.messages import HumanMessage

        class _Exploding:
            async def astream(self, _messages: object) -> object:
                yield AIMessageChunkLike("partial ")
                raise _StatusError(503)

        provider = self._provider_with(_Exploding())
        received: list[str] = []
        with pytest.raises(LLMTransientError):
            async for chunk in provider.stream([HumanMessage(content="hi")]):
                received.append(chunk.text)
        assert received == ["partial "]


class AIMessageChunkLike:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage_metadata = None


class TestUserLLM:
    """Models built from a key a user saved (ADR-0020). Constructed, never called."""

    def test_an_openai_compatible_preset_is_sent_to_its_own_url(self) -> None:
        from langchain_openai import ChatOpenAI

        llm = build_user_llm(
            PRESETS["groq"], model="llama-3.3-70b-versatile", api_key="gsk-x", settings=Settings()
        )

        assert isinstance(llm, LangChainLLM)
        client = llm._client
        assert isinstance(client, ChatOpenAI)
        assert client.openai_api_base == "https://api.groq.com/openai/v1"
        assert client.model_name == "llama-3.3-70b-versatile"
        assert isinstance(client.openai_api_key, SecretStr)
        assert client.openai_api_key.get_secret_value() == "gsk-x"
        # The server's temperature is for the server's model; some models
        # refuse anything but their default.
        assert client.temperature is None
        assert client.max_retries == 0
        assert llm._structured_method == "function_calling"

    def test_anthropic_uses_its_native_client(self) -> None:
        from langchain_anthropic import ChatAnthropic

        llm = build_user_llm(
            PRESETS["anthropic"], model="claude-sonnet-5-5", api_key="sk-ant-x", settings=Settings()
        )

        assert isinstance(llm, LangChainLLM)
        assert isinstance(llm._client, ChatAnthropic)
        assert llm._client.anthropic_api_url == "https://api.anthropic.com"
        assert llm._client.max_retries == 0
        assert llm.model == "claude-sonnet-5-5"

    def test_openai_keeps_strict_json_schema_output(self) -> None:
        llm = build_user_llm(
            PRESETS["openai"], model="gpt-4.1", api_key="sk-x", settings=Settings()
        )
        assert isinstance(llm, LangChainLLM)
        assert llm._structured_method == "json_schema"

    async def test_a_rejected_user_key_is_the_users_problem(self) -> None:
        """The user can fix their own key; the run must say so, not blame the server."""
        from langchain_core.messages import HumanMessage

        class _Unauthorised:
            def astream(self, _messages: object) -> object:
                raise _StatusError(401)

        user = LangChainLLM(_Unauthorised(), model="m", max_output_tokens=10, user_key=True)
        server = LangChainLLM(_Unauthorised(), model="m", max_output_tokens=10)

        with pytest.raises(LLMKeyRejected):
            async for _ in user.stream([HumanMessage(content="hi")]):
                pass
        with pytest.raises(LLMAuthError) as caught:
            async for _ in server.stream([HumanMessage(content="hi")]):
                pass
        assert not isinstance(caught.value, LLMKeyRejected)


async def test_a_missing_user_model_is_its_own_error() -> None:
    """Users type model names; a 404 tells them to pick another."""
    from langchain_core.messages import HumanMessage

    class _NotFound:
        def astream(self, _messages: object) -> object:
            raise _StatusError(404)

    user = LangChainLLM(_NotFound(), model="m", max_output_tokens=10, user_key=True)
    server = LangChainLLM(_NotFound(), model="m", max_output_tokens=10)

    with pytest.raises(LLMModelUnavailable):
        async for _ in user.stream([HumanMessage(content="hi")]):
            pass
    with pytest.raises(LLMPermanentError) as caught:
        async for _ in server.stream([HumanMessage(content="hi")]):
            pass
    assert not isinstance(caught.value, LLMModelUnavailable)
