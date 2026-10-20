"""Document upload, listing, and deletion (ADR-0015).

An upload is checked from cheapest to dearest, so a request that will be
refused is refused before it costs anything: ownership, then the bytes
themselves, then whether this exact file is already here, then the document
cap, and only then parsing in a child process, chunking, and the embedding
quota. The PDF bytes are never stored; the chunks are what remains.

Embedding happens afterwards, in the background (``ingestion.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    AppError,
    DocumentLimitReached,
    DocumentNotFound,
    EmailNotVerified,
    EmbeddingQuotaExceeded,
    PdfEncrypted,
    PdfInvalid,
    PdfNoText,
    PdfTooComplex,
    PdfTooLarge,
    RunInProgress,
    ServiceUnavailable,
)
from app.core import telemetry
from app.core.config import Settings
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.models.user import User
from app.repositories.documents import CHARS_PER_TOKEN, DocumentRepository
from app.repositories.runs import RunRepository
from app.services.conversations import ConversationService
from app.services.pdf import PdfError, PdfParser, PdfParserBusy
from app.services.pdf_worker import ParsedPdf

logger = logging.getLogger(__name__)

QUOTA_WINDOW = timedelta(hours=24)

# The PDF specification lets the header start anywhere in the first 1024
# bytes; readers in the wild accept that, so the check does too.
PDF_MAGIC = b"%PDF-"
PDF_HEADER_WINDOW = 1024

FILENAME_MAX_LENGTH = 255
DEFAULT_FILENAME = "document.pdf"

# How long to tell a client to wait when every parse slot is taken.
PARSER_BUSY_RETRY_SECONDS = 5


class Waker(Protocol):
    def wake(self) -> None: ...


@dataclass(frozen=True, slots=True)
class UploadResult:
    document: Document
    # False when this exact file was already in the conversation.
    created: bool


def sanitize_filename(raw: str | None) -> str:
    """A display name that is safe to store, show, and put in a prompt.

    Never used as a path, since no file is written. Still reduced to a base
    name, because clients disagree about sending one and a full local path can
    reveal a user name. Control characters go: a filename reaches the model as
    a source title, and a newline there could break the source fence.
    """
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in name)
    name = " ".join(name.split())
    if not name:
        return DEFAULT_FILENAME
    if len(name) > FILENAME_MAX_LENGTH:
        stem, dot, extension = name.rpartition(".")
        if dot and 0 < len(extension) <= 10:
            name = stem[: FILENAME_MAX_LENGTH - len(extension) - 1] + "." + extension
        else:
            name = name[:FILENAME_MAX_LENGTH]
    return name


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


_PDF_ERRORS: dict[str, type[AppError]] = {
    "pdf_invalid": PdfInvalid,
    "pdf_encrypted": PdfEncrypted,
    "pdf_no_text": PdfNoText,
    "pdf_too_large": PdfTooLarge,
    "pdf_too_complex": PdfTooComplex,
}


class DocumentService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        parser: PdfParser | None = None,
        ingestion: Waker | None = None,
        embedding_model: str = "",
        embedding_dimensions: int = 0,
    ) -> None:
        self._session = session
        self._settings = settings
        self._parser = parser
        self._ingestion = ingestion
        self._embedding_model = embedding_model
        self._embedding_dimensions = embedding_dimensions
        self._documents = DocumentRepository(session)
        self._conversations = ConversationService(session)

    # --- upload ------------------------------------------------------------

    async def check_can_upload(self, *, user: User, conversation_id: uuid.UUID) -> None:
        """Everything that can be decided before the body is read.

        The route calls this first, so an unverified account or a guessed
        conversation id is turned away without the server receiving 10 MB.
        """
        if self._settings.require_email_verification and not user.is_verified:
            raise EmailNotVerified("Verify your email address to upload documents.")
        await self._conversations.get(user=user, conversation_id=conversation_id)

    async def upload(
        self, *, user: User, conversation_id: uuid.UUID, filename: str | None, data: bytes
    ) -> UploadResult:
        """Store a PDF's text for the conversation; every outcome is counted."""
        try:
            result = await self._upload(
                user=user, conversation_id=conversation_id, filename=filename, data=data
            )
        except AppError as exc:
            telemetry.record_document_upload(exc.code)
            raise
        telemetry.record_document_upload("accepted" if result.created else "duplicate")
        return result

    async def _upload(
        self, *, user: User, conversation_id: uuid.UUID, filename: str | None, data: bytes
    ) -> UploadResult:
        await self.check_can_upload(user=user, conversation_id=conversation_id)
        self._check_bytes(data)

        # In a thread: hashlib releases the GIL for large inputs, and 10 MiB
        # took 33 ms that the event loop would otherwise have waited out.
        sha256 = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
        existing = await self._documents.find_by_sha(
            user_id=user.id, conversation_id=conversation_id, content_sha256=sha256
        )
        if existing is not None:
            # The same file again: nothing to parse, nothing to pay for.
            return UploadResult(existing, created=False)

        limit = self._settings.max_documents_per_conversation
        if await self._documents.count_for_conversation(conversation_id=conversation_id) >= limit:
            raise DocumentLimitReached(
                f"A conversation can hold at most {limit} documents.", details={"limit": limit}
            )

        # Parsed and chunked in a child process (pdf_worker.py).
        parsed = await self._parse(data)
        chunks = parsed.chunks
        if len(chunks) > self._settings.max_chunks_per_document:
            raise PdfTooLarge(
                details={"limit": "chunks", "max": self._settings.max_chunks_per_document}
            )
        char_count = sum(len(content) for _, content in chunks)
        await self._check_quota(user, estimate_tokens(char_count))

        document = Document(
            id=uuid.uuid4(),
            user_id=user.id,
            conversation_id=conversation_id,
            filename=sanitize_filename(filename),
            content_sha256=sha256,
            byte_size=len(data),
            page_count=parsed.page_count,
            char_count=char_count,
            chunk_count=len(chunks),
            status=DocumentStatus.processing,
            embedding_model=self._embedding_model,
            embedding_dimensions=self._embedding_dimensions,
        )
        rows = [
            DocumentChunk(
                document_id=document.id,
                user_id=user.id,
                conversation_id=conversation_id,
                ordinal=ordinal,
                page=page,
                content=content,
            )
            for ordinal, (page, content) in enumerate(chunks)
        ]
        self._documents.add(document, rows)
        try:
            await self._session.commit()
        except IntegrityError:
            # The same file, uploaded twice at once: the other request won.
            await self._session.rollback()
            winner = await self._documents.find_by_sha(
                user_id=user.id, conversation_id=conversation_id, content_sha256=sha256
            )
            if winner is None:
                raise
            return UploadResult(winner, created=False)

        await self._session.refresh(document)
        if self._ingestion is not None:
            self._ingestion.wake()
        logger.info(
            "document_uploaded",
            extra={
                "document_id": document.id,
                "conversation_id": conversation_id,
                "pages": document.page_count,
                "chunks": document.chunk_count,
                "bytes": document.byte_size,
            },
        )
        return UploadResult(document, created=True)

    def _check_bytes(self, data: bytes) -> None:
        if len(data) > self._settings.max_document_bytes:
            raise PdfTooLarge(details={"limit": "bytes", "max": self._settings.max_document_bytes})
        if PDF_MAGIC not in data[:PDF_HEADER_WINDOW]:
            # Content-Type and the extension are the client's claims; the
            # bytes are the file's.
            raise PdfInvalid

    async def _parse(self, data: bytes) -> ParsedPdf:
        if self._parser is None:  # pragma: no cover - wiring error
            raise RuntimeError("DocumentService.upload needs a PdfParser")
        try:
            return await self._parser.parse(data)
        except PdfParserBusy:
            raise ServiceUnavailable(
                "Too many documents are being read right now. Try again shortly.",
                headers={"Retry-After": str(PARSER_BUSY_RETRY_SECONDS)},
                details={"retry_after_seconds": PARSER_BUSY_RETRY_SECONDS},
            ) from None
        except PdfError as exc:
            error = _PDF_ERRORS[exc.code]
            details = None
            if exc.code == "pdf_too_large" and exc.limit is not None:
                maximum = {
                    "pages": self._settings.max_document_pages,
                    "characters": self._settings.max_document_chars,
                }.get(exc.limit)
                details = {"limit": exc.limit, "max": maximum}
            raise error(details=details) from None

    async def _check_quota(self, user: User, needed: int) -> None:
        """Refuse an upload that would take the user past the daily allowance.

        Counts what was spent in the window, what unfinished documents are
        still expected to spend, and this upload's estimate.
        """
        limit = self._settings.daily_embedding_token_limit_per_user
        now = datetime.now(UTC)
        spent, oldest = await self._documents.tokens_spent_since(
            user_id=user.id, since=now - QUOTA_WINDOW
        )
        pending = await self._documents.tokens_pending(user_id=user.id)
        if spent + pending + needed <= limit:
            return
        retry_after = (
            max(1, math.ceil((oldest + QUOTA_WINDOW - now).total_seconds()))
            if oldest is not None
            else math.ceil(self._settings.document_ingestion_sweep_seconds)
        )
        telemetry.record_rate_limited("embedding_quota")
        logger.info("document_rejected_quota", extra={"limit": limit, "needed": needed})
        raise EmbeddingQuotaExceeded(
            headers={"Retry-After": str(retry_after)},
            details={"limit": limit, "retry_after_seconds": retry_after},
        )

    # --- read --------------------------------------------------------------

    async def list(self, *, user: User, conversation_id: uuid.UUID) -> Sequence[Document]:
        await self._conversations.get(user=user, conversation_id=conversation_id)
        return await self._documents.list_for_conversation(
            user_id=user.id, conversation_id=conversation_id
        )

    async def get(
        self, *, user: User, conversation_id: uuid.UUID, document_id: uuid.UUID
    ) -> Document:
        await self._conversations.get(user=user, conversation_id=conversation_id)
        document = await self._documents.get(
            user_id=user.id, conversation_id=conversation_id, document_id=document_id
        )
        if document is None:
            raise DocumentNotFound
        return document

    # --- delete ------------------------------------------------------------

    async def delete(
        self, *, user: User, conversation_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        """Hard-delete the document and its chunks.

        Refused while the conversation is answering a message, so a run never
        cites a chunk that vanished under it. The caller then erases the
        conversation's checkpoints, which may hold the document's text.
        """
        await self.get(user=user, conversation_id=conversation_id, document_id=document_id)
        if await RunRepository(self._session).get_active(conversation_id=conversation_id):
            raise RunInProgress("Wait for the current answer to finish before removing a document.")
        await self._documents.delete(
            user_id=user.id, conversation_id=conversation_id, document_id=document_id
        )
        await self._session.commit()
        logger.info(
            "document_deleted",
            extra={"document_id": document_id, "conversation_id": conversation_id},
        )
