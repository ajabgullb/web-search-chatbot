"""Chat completion, behind an interface.

Two clients sit behind it: an OpenAI-compatible endpoint reached through
``langchain-openai`` (the base URL is configuration, so the same client serves
OpenAI, OpenRouter, Groq, Gemini, or a local Ollama), and Anthropic's native
API through ``langchain-anthropic``. Either may be the server's own model or a
user's, built from the key they saved (ADR-0020).

Nothing above this module may import a LangChain provider package or handle a
provider exception type. Failures leave here as :class:`LLMTransientError` (worth one
retry) or :class:`LLMPermanentError` (retrying only burns quota).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from langchain_core.messages import AnyMessage
from pydantic import BaseModel

from app.core.config import Settings
from app.providers.llm_catalog import LLMPreset, StructuredMethod

logger = logging.getLogger(__name__)

StructuredT = TypeVar("StructuredT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class LLMChunk:
    """One fragment of a streamed answer.

    Token usage arrives on whichever chunk the provider attaches it to —
    usually the last — so it is optional on every chunk rather than returned
    separately.
    """

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMError(Exception):
    """Base for every failure this module raises."""


class LLMTransientError(LLMError):
    """Timeout, connection reset, or a 5xx/429 — one bounded retry is sane."""


class LLMPermanentError(LLMError):
    """Bad credentials, a rejected request, or exhausted quota."""


class LLMAuthError(LLMPermanentError):
    """The provider rejected our credentials (401/403).

    Split out from other permanent failures because the fix is an operator's,
    not a user's: an outage clears up on its own, a revoked key never does.
    """


class LLMKeyRejected(LLMAuthError):
    """The provider rejected a key a *user* saved.

    Unlike :class:`LLMAuthError`, the user can fix this one, so the run says
    so plainly and nothing is logged as an operator problem.
    """


class LLMModelUnavailable(LLMPermanentError):
    """A user's provider answered 404: usually a model name it does not have.

    Users type model names, so this is theirs to fix by picking another.
    """


class LLMProvider(Protocol):
    @property
    def model(self) -> str: ...

    def stream(
        self, messages: Sequence[AnyMessage], *, max_output_tokens: int | None = None
    ) -> AsyncIterator[LLMChunk]:
        """Yield the answer as it is generated.

        Answers are always streamed, even when nobody is watching: one code
        path for the user-facing answer means the thing tested is the thing
        that ships.
        """
        ...

    async def structured(
        self, messages: Sequence[AnyMessage], schema: type[StructuredT]
    ) -> StructuredT: ...


# HTTP statuses worth retrying. Everything else is the caller's fault or a
# quota wall, and a retry would just pay for the same error twice.
_TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_AUTH_STATUSES = frozenset({401, 403})

# Matched against the exception class name so that this module does not import
# the provider SDK's exception hierarchy just to classify a failure.
_TRANSIENT_NAMES = ("timeout", "connection", "apiconnection", "internalserver")


def classify_llm_error(exc: Exception) -> LLMError:
    """Map an arbitrary provider exception onto the retry decision.

    Classification is by HTTP status where one is available and by exception
    name otherwise. Anything unrecognised is treated as permanent: retrying an
    error we do not understand is how a bad request becomes a bill.
    """
    if isinstance(exc, TimeoutError):
        return LLMTransientError("llm request timed out")

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in _TRANSIENT_STATUSES:
            return LLMTransientError(f"llm provider returned {status}")
        if status in _AUTH_STATUSES:
            return LLMAuthError(f"llm provider rejected the credentials ({status})")
        return LLMPermanentError(f"llm provider returned {status}")

    name = type(exc).__name__.lower()
    if any(token in name for token in _TRANSIENT_NAMES):
        return LLMTransientError(f"llm transport failure: {type(exc).__name__}")
    return LLMPermanentError(f"llm call failed: {type(exc).__name__}")


class LangChainLLM:
    """Any LangChain chat model, adapted to :class:`LLMProvider`.

    The provider-specific part is only how the client is constructed; the
    streaming, structured output, and error classification are the same.
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        max_output_tokens: int,
        structured_method: StructuredMethod | None = None,
        user_key: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens
        # None keeps LangChain's default for the client.
        self._structured_method = structured_method
        self._user_key = user_key

    @property
    def model(self) -> str:
        return self._model

    def _classify(self, exc: Exception) -> LLMError:
        error = classify_llm_error(exc)
        if self._user_key:
            if isinstance(error, LLMAuthError):
                return LLMKeyRejected(str(error))
            if getattr(exc, "status_code", None) == 404:
                return LLMModelUnavailable(str(error))
        return error

    async def stream(
        self, messages: Sequence[AnyMessage], *, max_output_tokens: int | None = None
    ) -> AsyncIterator[LLMChunk]:
        client = self._client
        if max_output_tokens is not None and max_output_tokens != self._max_output_tokens:
            client = client.bind(max_tokens=max_output_tokens)
        try:
            async for chunk in client.astream(list(messages)):
                usage = getattr(chunk, "usage_metadata", None) or {}
                yield LLMChunk(
                    text=_text_of(chunk),
                    prompt_tokens=usage.get("input_tokens"),
                    completion_tokens=usage.get("output_tokens"),
                )
        except Exception as exc:  # re-raised as our own type below
            # A failure mid-stream is classified exactly like one before the
            # first token: the caller decides what a partial answer is worth.
            raise self._classify(exc) from exc

    async def structured(
        self, messages: Sequence[AnyMessage], schema: type[StructuredT]
    ) -> StructuredT:
        kwargs: dict[str, Any] = {}
        if self._structured_method is not None:
            kwargs["method"] = self._structured_method
        try:
            result = await self._client.with_structured_output(schema, **kwargs).ainvoke(
                list(messages)
            )
        except Exception as exc:  # re-raised as our own type below
            raise self._classify(exc) from exc
        if not isinstance(result, schema):
            raise LLMPermanentError(f"model did not return {schema.__name__}")
        return result


def _openai_client(
    *,
    model: str,
    api_key: str,
    base_url: str,
    timeout: float,
    max_output_tokens: int,
    temperature: float | None,
) -> Any:
    # Imported lazily so that a test suite using the fake provider does not
    # pay the import cost of the whole LangChain OpenAI stack.
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        api_key=api_key or "not-set",
        base_url=base_url,
        timeout=timeout,
        temperature=temperature,
        max_tokens=max_output_tokens,
        max_retries=0,  # retries are the graph's decision, not the SDK's
        # Ask for token usage on the final streamed chunk. Without it a
        # streamed answer has no usage at all, and cost goes unrecorded.
        stream_usage=True,
    )


class OpenAICompatibleLLM(LangChainLLM):
    """The server's own model: chat completions against any OpenAI-compatible
    ``/v1`` endpoint, configured from ``LLM_*`` settings."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            _openai_client(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout_seconds,
                max_output_tokens=settings.llm_max_output_tokens,
                temperature=settings.llm_temperature,
            ),
            model=settings.llm_model,
            max_output_tokens=settings.llm_max_output_tokens,
        )


def build_user_llm(
    preset: LLMPreset, *, model: str, api_key: str, settings: Settings
) -> LLMProvider:
    """A model reached with a key a user saved.

    Built per run. That is cheap: both LangChain packages share one HTTP
    connection pool per base URL across instances, so this opens no new pool.

    No temperature is sent. The server's ``LLM_TEMPERATURE`` was chosen for
    its own model, and some models a user may pick (OpenAI's reasoning
    models among them) reject any temperature but their default.
    """
    client: Any
    if preset.kind == "anthropic":
        from langchain_anthropic import ChatAnthropic

        client = ChatAnthropic(
            model=model,
            api_key=api_key,
            base_url=preset.base_url,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_output_tokens,
            max_retries=0,
            stream_usage=True,
        )
    else:
        client = _openai_client(
            model=model,
            api_key=api_key,
            base_url=preset.base_url,
            timeout=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            temperature=None,
        )
    return LangChainLLM(
        client,
        model=model,
        max_output_tokens=settings.llm_max_output_tokens,
        structured_method=preset.structured_method,
        user_key=True,
    )


def _text_of(response: object) -> str:
    """Flatten a message's content to plain text.

    Content may arrive as a string or as a list of typed blocks depending on
    the model; the graph only ever wants the text.
    """
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    """The server's own model, or None when ``LLM_API_KEY`` is empty.

    None is a supported configuration (ADR-0020): users then bring their own
    keys, and a user without one is asked for a key instead of getting a run
    that fails on the provider's 401.
    """
    if settings.llm_provider != "openai_compatible":
        raise ValueError(
            f"Unsupported LLM_PROVIDER {settings.llm_provider!r}; only 'openai_compatible' exists"
        )
    if not settings.llm_api_key:
        return None
    return OpenAICompatibleLLM(settings)
