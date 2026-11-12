"""Users' own LLM API keys: saving, choosing, and resolving a run's model (ADR-0020).

A user may save one key per provider and switch between them, the way the
model picker in the chat box lets them. Which model answers is decided by
one rule, :func:`choose`, used both to show the choice and to make it:

1. The user's active key, with the model they picked for it.
2. Otherwise the server's model (``LLM_API_KEY``), if the operator set one.
3. Otherwise the user's most recently checked key, if they have any.
4. Otherwise nothing, and the run is refused with ``llm_key_required``
   before a message is stored.

A saved key is never returned by the API, logged, or put in graph state or a
trace. It is decrypted only to build the run's client, which holds it in
memory for the length of the run.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    LLMCredentialNotFound,
    LLMDefaultUnavailable,
    LLMKeyCheckUnavailable,
    LLMKeyInvalid,
    LLMKeyUnreadable,
    LLMModelRejected,
    LLMProviderUnknown,
)
from app.core.config import Settings
from app.core.crypto import DecryptionError, SecretBox
from app.core.logging import user_ref
from app.models.llm_credential import LLMCredential
from app.providers.llm import LLMAuthError, LLMError, LLMProvider, LLMTransientError
from app.providers.llm_catalog import LLMPreset, get_preset
from app.repositories.llm_credentials import LLMCredentialRepository

logger = logging.getLogger(__name__)

# Builds the client for a user's key. Injected so tests substitute a fake and
# no test ever calls a real provider.
LLMFactory = Callable[[LLMPreset, str, str], LLMProvider]

# The check costs the user a handful of tokens. The reply itself is ignored:
# a call that returns at all proves the key, the model, and the account.
_PROBE_PROMPT = "Reply with the single word: OK"
_PROBE_MAX_TOKENS = 16
# Last characters shown back to the user. Four is what every provider's own
# dashboard shows, and far too few to help anyone guess the rest.
KEY_HINT_LENGTH = 4


def key_hint(api_key: str) -> str:
    return api_key[-KEY_HINT_LENGTH:]


def _associated_data(user_id: uuid.UUID) -> bytes:
    """Binds a ciphertext to its owner; see :mod:`app.core.crypto`."""
    return b"llm_credentials:" + user_id.bytes


@dataclass(frozen=True, slots=True)
class Selection:
    """Which model answers this user's next run, before any key is decrypted."""

    source: Literal["user", "server"]
    model: str
    # The preset id for a user's key; None for the server's model.
    provider: str | None = None
    credential: LLMCredential | None = None


def choose(credentials: Sequence[LLMCredential], server_model: str | None) -> Selection | None:
    """The rule in the module docstring. ``credentials`` newest-checked first."""
    usable = [credential for credential in credentials if get_preset(credential.provider)]
    active = next((credential for credential in usable if credential.is_active), None)
    if active is not None:
        return Selection("user", active.model, active.provider, active)
    if server_model is not None:
        return Selection("server", server_model)
    if usable:
        # Nothing chosen and no server model: any key the user saved beats
        # refusing to answer.
        fallback = usable[0]
        return Selection("user", fallback.model, fallback.provider, fallback)
    return None


@dataclass(frozen=True, slots=True)
class ResolvedLLM:
    """The model a run will use, and where it came from."""

    provider: LLMProvider
    # Recorded on the run: a preset id ("openai") for a user's key, or
    # LLM_PROVIDER for the server's model.
    provider_id: str
    model: str
    source: Literal["user", "server"]

    @property
    def metrics_label(self) -> str:
        """The ``model`` label on token metrics.

        A user's model name is free text, and a Prometheus label with
        unbounded values grows a new series per distinct name. So a user's
        model is counted under its preset only.
        """
        return self.model if self.source == "server" else f"user:{self.provider_id}"


class LLMCredentialService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        box: SecretBox,
        factory: LLMFactory,
    ) -> None:
        self._session = session
        self._settings = settings
        self._box = box
        self._factory = factory
        self._credentials = LLMCredentialRepository(session)

    async def list(self, *, user_id: uuid.UUID) -> Sequence[LLMCredential]:
        return await self._credentials.list_for_user(user_id=user_id)

    async def save_key(
        self, *, user_id: uuid.UUID, provider: str, model: str, api_key: str
    ) -> None:
        """Check the key against the provider, store it encrypted, and switch
        to it: adding a key is how a user says "answer with this"."""
        preset = get_preset(provider)
        if preset is None:
            raise LLMProviderUnknown

        # No transaction may sit open across the network call: a connection
        # idle-in-transaction for as long as the provider takes to answer.
        await self._session.rollback()
        await self._probe(preset, model, api_key)

        await self._credentials.upsert(
            user_id=user_id,
            provider=preset.id,
            model=model,
            encrypted_api_key=self._box.seal(api_key, associated_data=_associated_data(user_id)),
            key_hint=key_hint(api_key),
            verified_at=datetime.now(UTC),
        )
        await self._credentials.activate(user_id=user_id, provider=preset.id, model=model)
        await self._session.commit()
        logger.info(
            "llm_credential_saved", extra={"user": user_ref(user_id), "provider": preset.id}
        )

    async def select(
        self,
        *,
        user_id: uuid.UUID,
        provider: str | None,
        model: str | None,
        server_model: str | None,
    ) -> None:
        """Switch the active model. No provider call: the key was checked when
        saved, and switching should be instant. ``provider`` None picks the
        server's model."""
        if provider is None:
            if server_model is None:
                raise LLMDefaultUnavailable
        elif get_preset(provider) is None:
            raise LLMProviderUnknown
        if not await self._credentials.activate(user_id=user_id, provider=provider, model=model):
            await self._session.rollback()
            raise LLMCredentialNotFound
        await self._session.commit()

    async def delete(self, *, user_id: uuid.UUID, provider: str) -> None:
        if await self._credentials.delete(user_id=user_id, provider=provider):
            logger.info(
                "llm_credential_deleted", extra={"user": user_ref(user_id), "provider": provider}
            )
        await self._session.commit()

    async def resolve(
        self, *, user_id: uuid.UUID, server_llm: LLMProvider | None
    ) -> ResolvedLLM | None:
        """The model this user's next run will use, or None if there is none."""
        credentials = await self._credentials.list_for_user(user_id=user_id)
        selection = choose(credentials, server_llm.model if server_llm else None)
        if selection is None:
            return None
        if selection.credential is None:
            assert server_llm is not None
            return ResolvedLLM(
                provider=server_llm,
                provider_id=self._settings.llm_provider,
                model=server_llm.model,
                source="server",
            )
        credential = selection.credential
        preset = get_preset(credential.provider)
        assert preset is not None  # choose() skips unknown presets
        return ResolvedLLM(
            provider=self._factory(preset, credential.model, self._decrypt(credential)),
            provider_id=preset.id,
            model=credential.model,
            source="user",
        )

    def _decrypt(self, credential: LLMCredential) -> str:
        try:
            return self._box.open(
                credential.encrypted_api_key,
                associated_data=_associated_data(credential.user_id),
            )
        except DecryptionError:
            # CREDENTIAL_ENCRYPTION_KEY was changed (or the row was tampered
            # with). Not silently replaced by the server's model: the user
            # chose their own and should know it stopped working.
            logger.warning(
                "llm_credential_undecryptable",
                extra={
                    "user": user_ref(credential.user_id),
                    "hint": "CREDENTIAL_ENCRYPTION_KEY changed since this key was saved",
                },
            )
            raise LLMKeyUnreadable from None

    async def _probe(self, preset: LLMPreset, model: str, api_key: str) -> None:
        llm = self._factory(preset, model, api_key)
        try:
            async with asyncio.timeout(self._settings.llm_key_check_timeout_seconds):
                async for _chunk in llm.stream(
                    [HumanMessage(content=_PROBE_PROMPT)], max_output_tokens=_PROBE_MAX_TOKENS
                ):
                    pass
        except TimeoutError:
            raise LLMKeyCheckUnavailable from None
        except LLMAuthError:
            raise LLMKeyInvalid from None
        except LLMTransientError:
            # Includes 429: the account may be out of credit or rate-limited.
            # Either way the key is not known to be bad, so nothing is saved
            # and the user is told to try again.
            raise LLMKeyCheckUnavailable from None
        except LLMError:
            raise LLMModelRejected from None
