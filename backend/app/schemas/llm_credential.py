"""AI provider settings: users' own LLM API keys (ADR-0020)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.providers.llm_catalog import MODEL_PATTERN, LLMPreset

API_KEY_MIN_LENGTH = 8
API_KEY_MAX_LENGTH = 512
# Printable ASCII, no spaces: every provider's key format fits, and nothing
# that could break a header or hide in a log line does.
_API_KEY_RE = re.compile(r"^[\x21-\x7E]+$")
# Path parameter shape; whether it names a preset is the service's check.
PROVIDER_PATTERN = r"^[a-z0-9_-]{1,32}$"


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


class LLMProviderRead(BaseModel):
    id: str
    label: str
    default_model: str
    suggested_models: list[str]
    key_url: str = Field(description="Where to create an API key for this provider.")

    @classmethod
    def build(cls, preset: LLMPreset) -> LLMProviderRead:
        return cls(
            id=preset.id,
            label=preset.label,
            default_model=preset.default_model,
            suggested_models=list(preset.suggested_models),
            key_url=preset.key_url,
        )


class LLMCredentialRead(BaseModel):
    """A saved key, described. The key itself is never returned."""

    model_config = ConfigDict(from_attributes=True)

    provider: str
    model: str = Field(description="The model last chosen for this provider.")
    key_hint: str = Field(description="The key's last four characters.")
    is_active: bool
    verified_at: datetime
    updated_at: datetime


class ServerModelRead(BaseModel):
    model: str


class LLMSelectionRead(BaseModel):
    """The model the user's next answer will come from."""

    source: Literal["user", "server"]
    provider: str | None = Field(description="Preset id for a user's key; null for the server's.")
    model: str


class LLMSettingsRead(BaseModel):
    providers: list[LLMProviderRead]
    credentials: list[LLMCredentialRead] = Field(description="The user's saved keys, described.")
    server_model: ServerModelRead | None = Field(
        description="The app's default model, or null if the server has none."
    )
    selection: LLMSelectionRead | None = Field(
        description="What answers next, or null: no key and no default, so runs are refused."
    )


class LLMCredentialWrite(BaseModel):
    """Save (or replace) the key for the provider in the path, and use it."""

    model: str = Field(min_length=1, max_length=128, pattern=MODEL_PATTERN)
    api_key: SecretStr = Field(
        min_length=API_KEY_MIN_LENGTH,
        max_length=API_KEY_MAX_LENGTH,
        description="The provider's API key. Never returned.",
    )

    @field_validator("model", "api_key", mode="before")
    @classmethod
    def _strip_pasted(cls, value: object) -> object:
        # Keys and model names are pasted, and pasting often brings whitespace.
        return _strip(value)

    @field_validator("api_key")
    @classmethod
    def _printable_key(cls, value: SecretStr) -> SecretStr:
        if not _API_KEY_RE.fullmatch(value.get_secret_value()):
            raise ValueError("must be printable characters with no spaces")
        return value


class LLMSelectionWrite(BaseModel):
    """Switch models. Both null picks the app's default model."""

    provider: str | None = Field(default=None, pattern=PROVIDER_PATTERN)
    model: str | None = Field(default=None, min_length=1, max_length=128, pattern=MODEL_PATTERN)

    @field_validator("model", mode="before")
    @classmethod
    def _strip_model(cls, value: object) -> object:
        return _strip(value)

    @model_validator(mode="after")
    def _both_or_neither(self) -> LLMSelectionWrite:
        if (self.provider is None) != (self.model is None):
            raise ValueError("Give both 'provider' and 'model', or neither for the default.")
        return self
