"""PDF documents scoped to one conversation (ADR-0015).

These routes authenticate with the bearer token, not the refresh cookie, so
like the run routes they need no Origin check.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.dependencies import (
    CurrentUserDep,
    DocumentServiceDep,
    SettingsDep,
    ThreadEraserDep,
    enforce_document_upload_limit,
)
from app.api.errors import ValidationFailed
from app.schemas.document import DocumentList, DocumentRead

router = APIRouter(prefix="/conversations/{conversation_id}/documents", tags=["documents"])

UPLOAD_FIELD = "file"

# Declared by hand because the handler parses the body itself (see below), so
# FastAPI cannot infer it. Without this the generated client would have no
# idea the route takes a file.
_UPLOAD_REQUEST_BODY: dict[str, Any] = {
    "required": True,
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": [UPLOAD_FIELD],
                "properties": {
                    UPLOAD_FIELD: {
                        "type": "string",
                        "format": "binary",
                        "description": "The PDF. At most MAX_DOCUMENT_BYTES (10 MiB by default).",
                    }
                },
            }
        }
    },
}

_FORM_HELP = f"Send the PDF as multipart/form-data in a field named {UPLOAD_FIELD!r}."


async def _read_upload(request: Request, *, max_bytes: int) -> tuple[str | None, bytes]:
    """The uploaded file's name and bytes, reading at most one byte past the limit.

    That one extra byte is how an oversized file is told apart from one exactly
    at the limit, without reading the rest of it.
    """
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise ValidationFailed(_FORM_HELP)
    try:
        async with request.form(max_files=1, max_fields=1) as form:
            upload = form.get(UPLOAD_FIELD)
            if not isinstance(upload, UploadFile):
                raise ValidationFailed(_FORM_HELP)
            return upload.filename, await upload.read(max_bytes + 1)
    except StarletteHTTPException as exc:
        # Starlette reports a malformed multipart body as a 400. Anything else
        # (the body-size limit's 413, in particular) passes through as is.
        if exc.status_code == status.HTTP_400_BAD_REQUEST:
            raise ValidationFailed(_FORM_HELP) from None
        raise


@router.post(
    "",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {
            "model": DocumentRead,
            "description": "This exact file was already in the conversation; it is returned as is.",
        }
    },
    openapi_extra={"requestBody": _UPLOAD_REQUEST_BODY},
    dependencies=[Depends(enforce_document_upload_limit)],
)
async def upload_document(
    conversation_id: uuid.UUID,
    request: Request,
    response: Response,
    current_user: CurrentUserDep,
    service: DocumentServiceDep,
    settings: SettingsDep,
) -> DocumentRead:
    """Upload a PDF. Returns at once with status ``processing``; poll until ``ready``.

    The body is read here, after authentication, the rate limit, and the
    ownership check have all passed. Declaring the file as a parameter would
    have FastAPI read and spool the whole body first, so an anonymous or
    throttled caller could still make the server receive 10 MB.
    """
    await service.check_can_upload(user=current_user, conversation_id=conversation_id)
    filename, data = await _read_upload(request, max_bytes=settings.max_document_bytes)
    result = await service.upload(
        user=current_user, conversation_id=conversation_id, filename=filename, data=data
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return DocumentRead.model_validate(result.document)


@router.get("", response_model=DocumentList)
async def list_documents(
    conversation_id: uuid.UUID, current_user: CurrentUserDep, service: DocumentServiceDep
) -> DocumentList:
    documents = await service.list(user=current_user, conversation_id=conversation_id)
    return DocumentList(items=[DocumentRead.model_validate(d) for d in documents])


@router.get("/{document_id}", response_model=DocumentRead)
async def get_document(
    conversation_id: uuid.UUID,
    document_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: DocumentServiceDep,
) -> DocumentRead:
    document = await service.get(
        user=current_user, conversation_id=conversation_id, document_id=document_id
    )
    return DocumentRead.model_validate(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    conversation_id: uuid.UUID,
    document_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: DocumentServiceDep,
    eraser: ThreadEraserDep,
) -> None:
    await service.delete(
        user=current_user, conversation_id=conversation_id, document_id=document_id
    )
    # The conversation's checkpoints may hold text retrieved from this
    # document. Each run rebuilds its transcript from the messages table, so
    # erasing the thread loses nothing the product shows.
    await eraser.adelete_thread(str(conversation_id))
