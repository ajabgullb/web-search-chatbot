"""The embedding provider boundary, and the fake that stands in for it.

No test reaches a network. Every response comes from an ``httpx2.MockTransport``
underneath the real ``openai`` SDK, so error bodies are parsed by the same code
that parses OpenAI's, and the classification under test is the one that ships.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable

import httpx2
import pytest
from pydantic import ValidationError

from app.core.config import EMBEDDING_VECTOR_DIMENSIONS, Settings
from app.providers.embeddings import (
    EmbeddingAuthError,
    EmbeddingPermanentError,
    EmbeddingTransientError,
    OpenAIEmbeddings,
    classify_embedding_error,
    validate_vectors,
)
from tests.fakes import FakeEmbeddings

DIMS = EMBEDDING_VECTOR_DIMENSIONS

Handler = Callable[[httpx2.Request], httpx2.Response]


def provider_with(
    handler: Handler, **overrides: object
) -> tuple[OpenAIEmbeddings, list[httpx2.Request]]:
    """A real provider whose HTTP traffic goes to ``handler``; also returns the requests seen."""
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    values: dict[str, object] = {"embedding_api_key": "sk-test", "embedding_batch_size": 4}
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(recording))
    return OpenAIEmbeddings(settings, http_client=client), seen


def embeddings_response(
    vectors: list[list[float]], *, tokens: int = 7, order: list[int] | None = None
) -> httpx2.Response:
    indices = order if order is not None else list(range(len(vectors)))
    return httpx2.Response(
        200,
        json={
            "object": "list",
            "model": "text-embedding-3-small",
            "data": [
                {"object": "embedding", "index": index, "embedding": vectors[index]}
                for index in indices
            ],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        },
    )


def error_response(
    status: int, code: str | None, kind: str = "invalid_request_error"
) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"message": "nope", "type": kind, "param": None, "code": code}}
    )


def unit(index: int) -> list[float]:
    vector = [0.0] * DIMS
    vector[index] = 1.0
    return vector


class TestRequestsAndResponses:
    async def test_vectors_come_back_in_input_order_with_billed_tokens(self) -> None:
        """The API labels each vector with its input index; order is checked, not assumed."""
        provider, seen = provider_with(
            lambda _: embeddings_response([unit(0), unit(1), unit(2)], tokens=11, order=[2, 0, 1])
        )
        batch = await provider.embed(["alpha", "beta", "gamma"])

        assert [vector.index(1.0) for vector in batch.vectors] == [0, 1, 2]
        assert batch.tokens == 11
        body = json.loads(seen[0].content)
        assert body["model"] == "text-embedding-3-small"
        assert body["input"] == ["alpha", "beta", "gamma"]
        assert body["dimensions"] == DIMS
        assert body["encoding_format"] == "float"
        assert seen[0].headers["authorization"] == "Bearer sk-test"
        await provider.aclose()

    async def test_the_embeddings_endpoint_is_not_the_chat_endpoint(self) -> None:
        """ADR-0015: LLM_BASE_URL may name a provider with no embeddings API."""
        provider, seen = provider_with(
            lambda _: embeddings_response([unit(0)]),
            llm_base_url="https://api.groq.com/openai/v1",
        )
        await provider.embed(["alpha"])
        assert str(seen[0].url) == "https://api.openai.com/v1/embeddings"
        await provider.aclose()

    async def test_a_missing_key_fails_without_a_request(self) -> None:
        provider, seen = provider_with(
            lambda _: embeddings_response([unit(0)]), embedding_api_key=""
        )
        with pytest.raises(EmbeddingAuthError, match="EMBEDDING_API_KEY"):
            await provider.embed(["alpha"])
        assert seen == []
        await provider.aclose()

    @pytest.mark.parametrize(
        ("texts", "complaint"),
        [([], "at least one"), (["a", "b", "c", "d", "e"], "batch limit"), (["ok", "  "], "empty")],
    )
    async def test_our_own_bad_input_is_a_programming_error_not_a_request(
        self, texts: list[str], complaint: str
    ) -> None:
        provider, seen = provider_with(lambda _: embeddings_response([unit(0)]))
        with pytest.raises(ValueError, match=complaint):
            await provider.embed(texts)
        assert seen == []
        await provider.aclose()

    async def test_a_vector_of_the_wrong_width_is_refused(self) -> None:
        """It could not be stored, and retrying will not change the model's answer."""
        provider, _ = provider_with(lambda _: embeddings_response([[0.1] * 512]))
        with pytest.raises(EmbeddingPermanentError, match="512 dimensions"):
            await provider.embed(["alpha"])
        await provider.aclose()

    async def test_a_missing_vector_is_refused(self) -> None:
        provider, _ = provider_with(lambda _: embeddings_response([unit(0)]))
        with pytest.raises(EmbeddingPermanentError, match="1 vectors for 2 texts"):
            await provider.embed(["alpha", "beta"])
        await provider.aclose()

    def test_a_non_finite_value_is_refused(self) -> None:
        """One NaN would poison every distance computed against the vector."""
        with pytest.raises(EmbeddingPermanentError, match="non-finite"):
            validate_vectors([[math.nan] + [0.0] * (DIMS - 1)], expected=1, dimensions=DIMS)


class TestErrorClassification:
    async def test_the_sdk_does_not_retry_behind_our_back(self) -> None:
        """The contract allows one retry, owned by the caller. A silent SDK retry
        would multiply it."""
        provider, seen = provider_with(lambda _: error_response(503, None, "server_error"))
        with pytest.raises(EmbeddingTransientError):
            await provider.embed(["alpha"])
        assert len(seen) == 1
        await provider.aclose()

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    async def test_retryable_statuses(self, status: int) -> None:
        provider, _ = provider_with(lambda _: error_response(status, None))
        with pytest.raises(EmbeddingTransientError):
            await provider.embed(["alpha"])
        await provider.aclose()

    @pytest.mark.parametrize("code", ["insufficient_quota", "billing_hard_limit_reached"])
    async def test_an_exhausted_account_is_permanent_despite_its_429(self, code: str) -> None:
        """OpenAI uses 429 for "slow down" and for "out of credit". Only the
        first clears on its own."""
        provider, _ = provider_with(lambda _: error_response(429, code, "insufficient_quota"))
        with pytest.raises(EmbeddingPermanentError) as caught:
            await provider.embed(["alpha"])
        assert not isinstance(caught.value, EmbeddingTransientError)
        await provider.aclose()

    async def test_a_plain_rate_limit_stays_transient(self) -> None:
        provider, _ = provider_with(
            lambda _: error_response(429, "rate_limit_exceeded", "requests")
        )
        with pytest.raises(EmbeddingTransientError):
            await provider.embed(["alpha"])
        await provider.aclose()

    @pytest.mark.parametrize("status", [401, 403])
    async def test_rejected_credentials_are_their_own_kind(self, status: int) -> None:
        provider, _ = provider_with(lambda _: error_response(status, "invalid_api_key"))
        with pytest.raises(EmbeddingAuthError):
            await provider.embed(["alpha"])
        await provider.aclose()

    @pytest.mark.parametrize("status", [400, 404, 422])
    async def test_rejected_requests_are_permanent(self, status: int) -> None:
        provider, _ = provider_with(lambda _: error_response(status, None))
        with pytest.raises(EmbeddingPermanentError) as caught:
            await provider.embed(["alpha"])
        assert not isinstance(caught.value, EmbeddingAuthError)
        await provider.aclose()

    async def test_a_dropped_connection_is_transient(self) -> None:
        def drop(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused", request=request)

        provider, _ = provider_with(drop)
        with pytest.raises(EmbeddingTransientError):
            await provider.embed(["alpha"])
        await provider.aclose()

    async def test_a_timeout_is_transient(self) -> None:
        def stall(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("too slow", request=request)

        provider, _ = provider_with(stall)
        with pytest.raises(EmbeddingTransientError):
            await provider.embed(["alpha"])
        await provider.aclose()

    def test_an_unrecognised_failure_defaults_to_permanent(self) -> None:
        assert isinstance(classify_embedding_error(ValueError("odd")), EmbeddingPermanentError)


class TestConfiguration:
    def test_dimensions_must_match_the_stored_vector_width(self) -> None:
        """A mismatch would otherwise surface as a failed insert on the first upload."""
        with pytest.raises(ValidationError, match="EMBEDDING_DIMENSIONS"):
            Settings(embedding_dimensions=512)

    def test_the_default_model_is_the_cheapest(self) -> None:
        # The declared default, not Settings(): a developer's .env may override it.
        assert Settings.model_fields["embedding_model"].default == "text-embedding-3-small"


class TestFakeEmbeddings:
    """The fake must behave enough like an embedder for retrieval tests to mean something."""

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    async def test_vectors_are_unit_length_and_deterministic(self) -> None:
        fake = FakeEmbeddings()
        first = (await fake.embed(["Quarterly revenue grew"])).vectors[0]
        second = (await FakeEmbeddings().embed(["Quarterly revenue grew"])).vectors[0]
        assert first == second
        assert len(first) == DIMS
        assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0)

    async def test_shared_words_score_higher_than_unrelated_text(self) -> None:
        fake = FakeEmbeddings()
        query, related, unrelated = (
            await fake.embed(
                [
                    "what was the revenue growth",
                    "revenue growth was twelve percent",
                    "the cat sat on the mat",
                ]
            )
        ).vectors
        assert self.cosine(query, related) > self.cosine(query, unrelated)

    async def test_failures_can_be_scripted(self) -> None:
        fake = FakeEmbeddings(error=EmbeddingTransientError("blip"), fail_times=1)
        with pytest.raises(EmbeddingTransientError):
            await fake.embed(["alpha"])
        assert len((await fake.embed(["alpha"])).vectors) == 1
