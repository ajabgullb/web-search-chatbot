"""Chat completion, behind an interface.

One implementation ships: an OpenAI-compatible endpoint reached through
``langchain-openai``. The base URL is configuration, so the same class serves
OpenAI, OpenRouter, Groq, or a local Ollama.

Nothing above this module may import ``langchain_openai`` or handle a provider
exception type. Failures leave here as :class:`LLMTransientError` (worth one
retry) or :class:`LLMPermanentError` (retrying only burns quota).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from langchain_core.messages import AnyMessage
from pydantic import BaseModel

from app.core.config import Settings

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
        return LLMPermanentError(f"llm provider returned {status}")

    name = type(exc).__name__.lower()
    if any(token in name for token in _TRANSIENT_NAMES):
        return LLMTransientError(f"llm transport failure: {type(exc).__name__}")
    return LLMPermanentError(f"llm call failed: {type(exc).__name__}")


class OpenAICompatibleLLM:
    """Chat completions against any OpenAI-compatible ``/v1`` endpoint."""

    def __init__(self, settings: Settings) -> None:
        # Imported lazily so that a test suite using the fake provider does not
        # pay the import cost of the whole LangChain OpenAI stack.
        from langchain_openai import ChatOpenAI

        self._model = settings.llm_model
        self._max_output_tokens = settings.llm_max_output_tokens
        self._client = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key or "not-set",
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_output_tokens,
            max_retries=0,  # retries are the graph's decision, not the SDK's
            # Ask for token usage on the final streamed chunk. Without it a
            # streamed answer has no usage at all, and cost goes unrecorded.
            stream_usage=True,
        )

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self, messages: Sequence[AnyMessage], *, max_output_tokens: int | None = None
    ) -> AsyncIterator[LLMChunk]:
        client = self._client
        if max_output_tokens is not None and max_output_tokens != self._max_output_tokens:
            client = client.bind(max_tokens=max_output_tokens)  # type: ignore[assignment]
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
            raise classify_llm_error(exc) from exc

    async def structured(
        self, messages: Sequence[AnyMessage], schema: type[StructuredT]
    ) -> StructuredT:
        try:
            result = await self._client.with_structured_output(schema).ainvoke(list(messages))
        except Exception as exc:  # re-raised as our own type below
            raise classify_llm_error(exc) from exc
        if not isinstance(result, schema):
            raise LLMPermanentError(f"model did not return {schema.__name__}")
        return result


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


def build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider != "openai_compatible":
        raise ValueError(
            f"Unsupported LLM_PROVIDER {settings.llm_provider!r}; only 'openai_compatible' exists"
        )
    return OpenAICompatibleLLM(settings)
