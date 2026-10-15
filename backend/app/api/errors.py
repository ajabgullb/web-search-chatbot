"""Stable error envelope and the exception handlers that produce it.

Every failure — expected or not — leaves the API in the same shape:

    {"error": {"code": ..., "message": ..., "request_id": ..., "details": ...}}

Clients switch on ``code``, which is part of the contract; ``message`` is for
humans and may be reworded. Nothing here ever leaks a stack trace, a driver
message, or a provider response body.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import get_request_id
from app.schemas.errors import ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for failures the API reports deliberately."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    message: str = "The request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.detail_message = message or self.message
        self.details = details
        self.headers = headers


class ValidationFailed(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_failed"
    message = "The request body failed validation."


class AuthenticationRequired(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_required"
    message = "Authentication is required."


class InvalidCredentials(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "invalid_credentials"
    message = "Email or password is incorrect."


class InvalidToken(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "invalid_token"
    message = "The token is missing, expired, or has already been used."


class AccountInactive(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "account_inactive"
    message = "This account is not active."


class OriginNotAllowed(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "origin_not_allowed"
    message = "The request origin is not allowed."


class EmailAlreadyRegistered(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "email_already_registered"
    message = "An account with that email already exists."


class RateLimited(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Try again shortly."


class DailyQuotaExceeded(AppError):
    """Distinct from ``rate_limited``: waiting a few seconds will not help, and
    the client should say so rather than offer an immediate retry."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "daily_quota_exceeded"
    message = "You have reached the daily message limit."


class EmailNotVerified(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "email_not_verified"
    message = "Verify your email address to start sending messages."


class PayloadTooLarge(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "payload_too_large"
    message = "The request body is too large."


class ServiceUnavailable(AppError):
    """A dependency the request needs is down. Retryable by definition."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "The service is temporarily unavailable. Try again shortly."


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Not found."


class ConversationNotFound(NotFound):
    """Raised for an unknown id *and* for one owned by somebody else.

    Deliberately indistinguishable: a distinct 403 would confirm that a guessed
    UUID names a real conversation.
    """

    code = "conversation_not_found"
    message = "Conversation not found."


class RunNotFound(NotFound):
    """Same rule as conversations: someone else's run is indistinguishable
    from a run that never existed."""

    code = "run_not_found"
    message = "Run not found."


class MessageNotFound(NotFound):
    code = "message_not_found"
    message = "Message not found."


class RunInProgress(AppError):
    """A conversation answers one message at a time."""

    status_code = status.HTTP_409_CONFLICT
    code = "run_in_progress"
    message = "This conversation is already answering a message."


class RetryNotAllowed(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "retry_not_allowed"
    message = "Only the latest message in a conversation can be retried."


class IdempotencyKeyReused(AppError):
    """The same key with a different request is a client bug, not a retry."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "idempotency_key_reused"
    message = "This Idempotency-Key was already used for a different request."


class MessageEmpty(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "message_empty"
    message = "The message is empty."


class MessageTooLong(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "message_too_long"
    message = "The message is too long."


class InvalidCursor(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_cursor"
    message = "The pagination cursor is not valid."


# Declared on the v1 router so the envelope is part of the published contract
# and lands in the generated TypeScript client. "default" covers every status
# not otherwise documented, which is exactly what this envelope does.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "default": {
        "model": ErrorResponse,
        "description": "Error envelope returned for every failure.",
    }
}


def _envelope(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=get_request_id(),
            details=details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        headers=headers,
    )


def error_response(exc: AppError) -> JSONResponse:
    """The envelope for ``exc``, for code outside the exception handlers
    (pure ASGI middleware that answers before routing)."""
    return _envelope(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.detail_message,
        details=exc.details,
        headers=exc.headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return _envelope(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.detail_message,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field paths and reasons are safe and genuinely useful to a client;
        # the raw input value is not echoed back.
        fields = [
            {"field": ".".join(str(part) for part in error["loc"][1:]), "reason": error["msg"]}
            for error in exc.errors()
        ]
        return _envelope(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ValidationFailed.code,
            message=ValidationFailed.message,
            details={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Covers 404s and the like raised by the framework itself.
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        headers = dict(exc.headers) if exc.headers else None
        return _envelope(status_code=exc.status_code, code=code, message=message, headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the traceback server-side; tell the client nothing but a code
        # and the request id they can quote in a support message.
        logger.exception(
            "unhandled_exception",
            extra={"path": request.url.path, "method": request.method},
            exc_info=exc,
        )
        return _envelope(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="An unexpected error occurred.",
        )


_STATUS_CODES = {
    status.HTTP_401_UNAUTHORIZED: "authentication_required",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_CONTENT_TOO_LARGE: "payload_too_large",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}
