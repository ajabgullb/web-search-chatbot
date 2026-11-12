"""The rule that decides which model answers (ADR-0020), without a database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.llm_credential import LLMCredential
from app.services.llm_credentials import Selection, choose


def credential(provider: str, *, active: bool = False, model: str = "m") -> LLMCredential:
    return LLMCredential(
        user_id=uuid.uuid4(),
        provider=provider,
        model=model,
        encrypted_api_key=b"",
        key_hint="abcd",
        is_active=active,
        verified_at=datetime.now(UTC),
    )


def test_the_active_key_wins_over_the_default() -> None:
    active = credential("anthropic", active=True, model="claude")
    chosen = choose([credential("openai"), active], "server-model")
    assert chosen == Selection("user", "claude", "anthropic", active)


def test_with_nothing_active_the_default_answers() -> None:
    assert choose([credential("openai")], "server-model") == Selection("server", "server-model")


def test_without_a_default_the_freshest_key_answers() -> None:
    newest = credential("groq", model="llama")
    assert choose([newest, credential("openai")], None) == Selection(
        "user", "llama", "groq", newest
    )


def test_nothing_at_all_means_no_model() -> None:
    assert choose([], None) is None


def test_a_key_for_a_removed_preset_is_ignored() -> None:
    gone = credential("retired-provider", active=True)
    assert choose([gone], "server-model") == Selection("server", "server-model")
    assert choose([gone], None) is None
