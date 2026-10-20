"""The one way embeddings are requested: timed, counted, retried once."""

from __future__ import annotations

import pytest
from pgvector import Vector
from prometheus_client import REGISTRY

from app.providers.embeddings import EmbeddingAuthError, EmbeddingTransientError
from app.repositories.documents import vector_literals
from app.services import embedding_calls
from app.services.embedding_calls import embed_with_retry
from tests.fakes import FakeEmbeddings


def count(purpose: str, outcome: str) -> float:
    labels = {"purpose": purpose, "outcome": outcome}
    return REGISTRY.get_sample_value("embedding_requests_total", labels) or 0.0


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedding_calls, "RETRY_BACKOFF_SECONDS", 0.0)


async def test_a_transient_failure_is_retried_once_and_both_calls_are_counted() -> None:
    fake = FakeEmbeddings(error=EmbeddingTransientError("503"), fail_times=1)
    before = (count("query", "transient_error"), count("query", "ok"))

    batch = await embed_with_retry(fake, ["hello"], purpose="query")

    assert len(batch.vectors) == 1
    assert len(fake.calls) == 2
    assert (count("query", "transient_error"), count("query", "ok")) == (
        before[0] + 1,
        before[1] + 1,
    )


async def test_two_transient_failures_give_up_after_the_retry() -> None:
    fake = FakeEmbeddings(error=EmbeddingTransientError("503"))
    with pytest.raises(EmbeddingTransientError):
        await embed_with_retry(fake, ["hello"], purpose="document")
    assert len(fake.calls) == 2


async def test_a_permanent_failure_is_never_retried() -> None:
    fake = FakeEmbeddings(error=EmbeddingAuthError("bad key"))
    before = count("document", "permanent_error")
    with pytest.raises(EmbeddingAuthError):
        await embed_with_retry(fake, ["hello"], purpose="document")
    assert len(fake.calls) == 1
    assert count("document", "permanent_error") == before + 1


def test_vector_literals_match_pgvectors_own_encoding() -> None:
    """Vectors are formatted off the event loop and cast in SQL; the text must
    be exactly what pgvector's encoder would have sent, or every stored vector
    would quietly differ from the one the provider returned."""
    vectors = [[0.1, -2.5, 3e-8, 1.0], [0.0] * 4, FakeEmbeddings(dimensions=8).vector("x y")]
    assert vector_literals(vectors) == [Vector._to_db(v) for v in vectors]
