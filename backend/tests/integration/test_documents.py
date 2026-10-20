"""PDF documents end to end: upload, ingestion, ownership, deletion.

Real app, real PostgreSQL with pgvector, real PDF parsing in child processes.
Only the embedding provider is a fake. The ingestion tests stop the
background loop and drive ``process_next`` themselves, so what happens and in
which order is decided by the test rather than by a race.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select, text, update

from app.core.config import Settings
from app.db.session import Database
from app.models import Document, DocumentChunk, DocumentStatus, EmbeddingUsage
from app.providers.embeddings import (
    EmbeddingAuthError,
    EmbeddingBatch,
    EmbeddingTransientError,
)
from app.repositories.documents import DocumentRepository, vector_literals
from app.services import embedding_calls
from tests.fakes import FakeEmbeddings, FakeLLM
from tests.integration.conftest import auth_header, create_conversation, register
from tests.integration.runs_support import install_providers, post_run, wait_for_active_run
from tests.pdfs import encrypted_pdf, make_pdf

pytestmark = pytest.mark.integration

REPORT = make_pdf(
    [
        "Quarterly report\n\nRevenue grew twelve percent.",
        "Costs fell by three percent.",
    ]
)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # Short waits, so the retry and give-up paths run in well under a second.
    settings.document_ingestion_sweep_seconds = 0.2
    settings.document_embed_max_attempts = 2
    return settings


@pytest.fixture
async def embeddings(app: FastAPI, client: AsyncClient) -> FakeEmbeddings:
    await app.state.embedding_provider.aclose()
    fake = FakeEmbeddings()
    app.state.embedding_provider = fake
    return fake


@pytest.fixture
async def headers(client: AsyncClient, embeddings: FakeEmbeddings) -> dict[str, str]:
    return auth_header(await register(client, email="reader@example.com"))


@pytest.fixture
async def conversation_id(client: AsyncClient, headers: dict[str, str]) -> str:
    return str((await create_conversation(client, headers))["id"])


@pytest.fixture
async def manual_ingestion(app: FastAPI, client: AsyncClient) -> Any:
    """Stop the background loop; the test calls ``process_next`` itself."""
    await app.state.ingestion.shutdown()
    return app.state.ingestion


def documents_url(conversation_id: str, document_id: str | None = None) -> str:
    base = f"/api/v1/conversations/{conversation_id}/documents"
    return f"{base}/{document_id}" if document_id else base


async def upload(
    client: AsyncClient,
    headers: dict[str, str],
    conversation_id: str,
    data: bytes = REPORT,
    *,
    filename: str = "report.pdf",
) -> Response:
    return await client.post(
        documents_url(conversation_id),
        files={"file": (filename, data, "application/pdf")},
        headers=headers,
    )


async def wait_for_status(
    client: AsyncClient,
    headers: dict[str, str],
    conversation_id: str,
    document_id: str,
    *,
    within: float = 15.0,
) -> dict[str, Any]:
    async with asyncio.timeout(within):
        while True:
            response = await client.get(
                documents_url(conversation_id, document_id), headers=headers
            )
            assert response.status_code == 200, response.text
            body: dict[str, Any] = response.json()
            if body["status"] != "processing":
                return body
            await asyncio.sleep(0.05)


async def count(app: FastAPI, model: Any, *conditions: Any) -> int:
    database: Database = app.state.database
    async with database.open_session() as session:
        return int(
            await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
        )


def error_code(response: Response) -> str:
    return str(response.json()["error"]["code"])


class RecordingEraser:
    def __init__(self) -> None:
        self.threads: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.threads.append(thread_id)


# --- upload ------------------------------------------------------------------


class TestUpload:
    async def test_an_upload_is_accepted_at_once_then_becomes_ready(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        response = await upload(client, headers, conversation_id, filename="C:\\docs\\Report.pdf")

        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["status"] == "processing"
        assert accepted["filename"] == "Report.pdf"
        assert (accepted["page_count"], accepted["chunk_count"]) == (2, 2)

        ready = await wait_for_status(client, headers, conversation_id, accepted["id"])
        assert ready["status"] == "ready"
        assert ready["error_code"] is None
        assert await count(app, DocumentChunk, DocumentChunk.embedding.is_(None)) == 0
        # The ledger holds what the provider said it billed.
        assert await count(app, EmbeddingUsage) == 1

    async def test_the_same_file_again_returns_the_existing_document(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        conversation_id: str,
        embeddings: FakeEmbeddings,
    ) -> None:
        first = (await upload(client, headers, conversation_id)).json()
        await wait_for_status(client, headers, conversation_id, first["id"])
        calls = len(embeddings.calls)

        again = await upload(client, headers, conversation_id, filename="copy.pdf")

        assert again.status_code == 200
        assert again.json()["id"] == first["id"]
        assert len(embeddings.calls) == calls  # nothing parsed or paid for twice

    async def test_documents_are_listed_in_upload_order(
        self, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        first = (await upload(client, headers, conversation_id, make_pdf(["one"]))).json()
        second = (await upload(client, headers, conversation_id, make_pdf(["two"]))).json()

        listed = (await client.get(documents_url(conversation_id), headers=headers)).json()

        assert [item["id"] for item in listed["items"]] == [first["id"], second["id"]]

    async def test_the_bytes_decide_what_a_file_is(
        self, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        """Content-Type and the extension are the client's claims."""
        response = await upload(client, headers, conversation_id, b"just some text")
        assert (response.status_code, error_code(response)) == (422, "pdf_invalid")

    @pytest.mark.parametrize(
        ("data", "code"),
        [
            (encrypted_pdf(["secret"], user_password="pw"), "pdf_encrypted"),
            (make_pdf(["", ""]), "pdf_no_text"),
        ],
    )
    async def test_unusable_pdfs_are_refused_with_a_reason(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        conversation_id: str,
        data: bytes,
        code: str,
    ) -> None:
        response = await upload(client, headers, conversation_id, data)
        assert (response.status_code, error_code(response)) == (422, code)
        assert await count(app, Document) == 0

    async def test_a_request_without_a_file_is_a_validation_error(
        self, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        as_json = await client.post(documents_url(conversation_id), json={}, headers=headers)
        wrong_field = await client.post(
            documents_url(conversation_id),
            files={"document": ("report.pdf", REPORT, "application/pdf")},
            headers=headers,
        )
        assert error_code(as_json) == error_code(wrong_field) == "validation_failed"

    async def test_an_oversized_file_is_refused(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        app.state.settings.max_document_bytes = len(REPORT) - 1

        response = await upload(client, headers, conversation_id)

        assert (response.status_code, error_code(response)) == (413, "pdf_too_large")
        assert response.json()["error"]["details"] == {
            "limit": "bytes",
            "max": len(REPORT) - 1,
        }

    async def test_only_the_upload_route_accepts_a_large_body(
        self, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        """Above the 256 KiB default, which still applies everywhere else."""
        large = make_pdf([f"Page {n}. " + "lorem ipsum dolor " * 110 for n in range(150)])
        assert len(large) > 262_144

        accepted = await upload(client, headers, conversation_id, large)
        elsewhere = await client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            content=large,
            headers={**headers, "Content-Type": "application/json", "Idempotency-Key": "k"},
        )

        assert accepted.status_code == 202, accepted.text
        assert (elsewhere.status_code, error_code(elsewhere)) == (413, "payload_too_large")

    async def test_an_unverified_account_cannot_upload(
        self, client: AsyncClient, embeddings: FakeEmbeddings
    ) -> None:
        unverified = auth_header(await register(client, email="new@example.com", verified=False))
        conversation = await create_conversation(client, unverified)

        response = await upload(client, unverified, str(conversation["id"]))

        assert (response.status_code, error_code(response)) == (403, "email_not_verified")


class TestLimits:
    async def test_the_document_cap_is_per_conversation(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        app.state.settings.max_documents_per_conversation = 1
        other = str((await create_conversation(client, headers))["id"])

        assert (await upload(client, headers, conversation_id, make_pdf(["a"]))).status_code == 202
        refused = await upload(client, headers, conversation_id, make_pdf(["b"]))
        elsewhere = await upload(client, headers, other, make_pdf(["b"]))

        assert (refused.status_code, error_code(refused)) == (409, "document_limit_reached")
        assert elsewhere.status_code == 202

    async def test_the_daily_embedding_quota_refuses_an_upload(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        app.state.settings.daily_embedding_token_limit_per_user = 5

        response = await upload(client, headers, conversation_id)

        assert (response.status_code, error_code(response)) == (429, "embedding_quota_exceeded")
        assert int(response.headers["Retry-After"]) >= 1

    async def test_deleting_a_document_does_not_refund_its_tokens(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        """Otherwise delete-and-reupload would reset the quota."""
        app.state.checkpointer = RecordingEraser()
        document = (await upload(client, headers, conversation_id)).json()
        await wait_for_status(client, headers, conversation_id, document["id"])
        spent = await count(app, EmbeddingUsage)

        await client.delete(documents_url(conversation_id, document["id"]), headers=headers)

        assert spent == 1
        assert await count(app, EmbeddingUsage) == spent

    async def test_uploads_are_rate_limited_per_user(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        app.state.settings.rate_limit_document_uploads = 1

        await upload(client, headers, conversation_id, make_pdf(["a"]))
        second = await upload(client, headers, conversation_id, make_pdf(["b"]))

        assert (second.status_code, error_code(second)) == (429, "rate_limited")


# --- ownership ---------------------------------------------------------------


async def test_another_users_documents_do_not_exist_for_them(
    client: AsyncClient, headers: dict[str, str], conversation_id: str
) -> None:
    document_id = (await upload(client, headers, conversation_id)).json()["id"]
    intruder = auth_header(await register(client, email="intruder@example.com"))
    own_conversation = str((await create_conversation(client, intruder))["id"])

    attempts = [
        await client.get(documents_url(conversation_id), headers=intruder),
        await client.get(documents_url(conversation_id, document_id), headers=intruder),
        await client.delete(documents_url(conversation_id, document_id), headers=intruder),
        await upload(client, intruder, conversation_id, make_pdf(["planted"])),
    ]
    # A real document id under the intruder's own conversation: still nothing.
    through_own = await client.get(documents_url(own_conversation, document_id), headers=intruder)

    assert {(r.status_code, error_code(r)) for r in attempts} == {(404, "conversation_not_found")}
    assert (through_own.status_code, error_code(through_own)) == (404, "document_not_found")
    still_there = await client.get(documents_url(conversation_id, document_id), headers=headers)
    assert still_there.status_code == 200


# --- deletion ----------------------------------------------------------------


class TestDeletion:
    async def test_removing_a_document_erases_its_text_and_the_threads_checkpoints(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        eraser = RecordingEraser()
        app.state.checkpointer = eraser
        document_id = (await upload(client, headers, conversation_id)).json()["id"]
        await wait_for_status(client, headers, conversation_id, document_id)

        response = await client.delete(documents_url(conversation_id, document_id), headers=headers)

        assert response.status_code == 204
        gone = await client.get(documents_url(conversation_id, document_id), headers=headers)
        assert gone.status_code == 404
        assert await count(app, DocumentChunk) == 0
        assert eraser.threads == [conversation_id]

    async def test_a_document_cannot_be_removed_while_an_answer_is_being_written(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        document_id = (await upload(client, headers, conversation_id)).json()["id"]
        await install_providers(app, llm=FakeLLM(answers=["slow"], delay_seconds=1.0))
        task = asyncio.create_task(post_run(client, headers, conversation_id, content="hi"))
        await wait_for_active_run(app, conversation_id)

        response = await client.delete(documents_url(conversation_id, document_id), headers=headers)

        assert (response.status_code, error_code(response)) == (409, "run_in_progress")
        await task

    async def test_deleting_a_conversation_hard_deletes_its_documents(
        self, app: FastAPI, client: AsyncClient, headers: dict[str, str], conversation_id: str
    ) -> None:
        eraser = RecordingEraser()
        app.state.checkpointer = eraser
        await upload(client, headers, conversation_id)

        response = await client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)

        assert response.status_code == 204
        assert await count(app, Document) == 0
        assert await count(app, DocumentChunk) == 0
        assert eraser.threads == [conversation_id]


# --- ingestion ---------------------------------------------------------------


async def status_of(app: FastAPI, document_id: str) -> tuple[DocumentStatus, str | None, int]:
    database: Database = app.state.database
    async with database.open_session() as session:
        document = await session.get(Document, uuid.UUID(document_id))
        assert document is not None
        return document.status, document.error_code, document.embedding_attempts


class TestIngestion:
    async def test_a_permanent_failure_fails_the_document_at_once(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        conversation_id: str,
        manual_ingestion: Any,
    ) -> None:
        app.state.embedding_provider = FakeEmbeddings(error=EmbeddingAuthError("bad key"))
        document_id = (await upload(client, headers, conversation_id)).json()["id"]

        assert await manual_ingestion.process_next() is True

        assert await status_of(app, document_id) == (
            DocumentStatus.failed,
            "embedding_unavailable",
            1,
        )
        failed = await client.get(documents_url(conversation_id, document_id), headers=headers)
        assert failed.json()["error_code"] == "embedding_unavailable"

    async def test_transient_failures_are_retried_then_given_up(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        conversation_id: str,
        manual_ingestion: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(embedding_calls, "RETRY_BACKOFF_SECONDS", 0.0)
        failing = FakeEmbeddings(error=EmbeddingTransientError("503"))
        app.state.embedding_provider = failing
        document_id = (await upload(client, headers, conversation_id)).json()["id"]

        await manual_ingestion.process_next()
        assert len(failing.calls) == 2  # the call, and its one retry
        # Backed off: nothing to claim until the sweep interval has passed.
        assert await manual_ingestion.process_next() is False

        await asyncio.sleep(0.25)
        await manual_ingestion.process_next()
        assert (await status_of(app, document_id))[0] is DocumentStatus.processing

        await asyncio.sleep(0.25)
        await manual_ingestion.process_next()  # third claim, over the limit of two
        assert await status_of(app, document_id) == (
            DocumentStatus.failed,
            "embedding_unavailable",
            3,
        )
        assert len(failing.calls) == 4  # the give-up claim spends nothing

    async def test_a_crashed_worker_is_resumed_from_the_first_missing_vector(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        conversation_id: str,
        manual_ingestion: Any,
        embeddings: FakeEmbeddings,
    ) -> None:
        document_id = uuid.UUID(
            (
                await upload(client, headers, conversation_id, make_pdf(["one", "two", "three"]))
            ).json()["id"]
        )
        database: Database = app.state.database
        # A worker claims the document, embeds the first chunk, and dies.
        async with database.open_session() as session:
            repository = DocumentRepository(session)
            claimed = await repository.claim_next(now=datetime.now(UTC), lease=timedelta(minutes=5))
            assert claimed is not None and claimed.id == document_id
            first = (await repository.pending_chunks(document_id=document_id, limit=1))[0]
            [literal] = vector_literals([embeddings.vector("one")])
            await repository.store_embeddings(literals=[(first.id, literal)])
            await session.commit()

        # Still leased to the dead worker: nobody else may take it yet.
        assert await manual_ingestion.process_next() is False

        async with database.open_session() as session:
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        assert await manual_ingestion.process_next() is True

        assert embeddings.calls == [["two", "three"]]
        assert await status_of(app, str(document_id)) == (DocumentStatus.ready, None, 2)

    async def test_deleting_while_a_batch_is_being_written_cannot_deadlock(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        conversation_id: str,
        manual_ingestion: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A batch and a delete lock the same rows; they must do it in the same order.

        The batch is paused inside its transaction, right after writing the
        vectors, until PostgreSQL reports the delete as waiting on a lock. The
        batch then finishes. Locking chunks before the document row (the old
        order) deadlocks here every time, and PostgreSQL aborts one side.
        """
        app.state.checkpointer = RecordingEraser()
        await upload(client, headers, conversation_id)
        database: Database = app.state.database
        original = DocumentRepository.store_embeddings
        deleting: list[asyncio.Task[Response]] = []

        async def delete_is_waiting_on_a_lock() -> bool:
            async with database.open_session() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND query LIKE 'DELETE FROM documents%'"
                    )
                )
            return bool(waiting)

        async def store_then_let_a_delete_in(self: DocumentRepository, **kwargs: Any) -> None:
            await original(self, **kwargs)
            deleting.append(
                asyncio.create_task(
                    client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)
                )
            )
            async with asyncio.timeout(5):
                while not await delete_is_waiting_on_a_lock():
                    await asyncio.sleep(0.02)

        monkeypatch.setattr(DocumentRepository, "store_embeddings", store_then_let_a_delete_in)

        assert await manual_ingestion.process_next() is True
        response = await deleting[0]

        assert response.status_code == 204, response.text
        assert await count(app, Document) == 0

    async def test_a_document_deleted_mid_embedding_is_not_an_error(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        conversation_id: str,
        manual_ingestion: Any,
    ) -> None:
        app.state.checkpointer = RecordingEraser()
        document_id = (await upload(client, headers, conversation_id)).json()["id"]

        class DeletesDuringCall(FakeEmbeddings):
            async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
                await client.delete(documents_url(conversation_id, document_id), headers=headers)
                return await super().embed(texts)

        app.state.embedding_provider = DeletesDuringCall()

        assert await manual_ingestion.process_next() is True
        assert await count(app, Document) == 0
        # The tokens were spent even though the document is gone.
        assert await count(app, EmbeddingUsage) == 1
