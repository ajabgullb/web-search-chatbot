"""The LLM providers a user may bring their own key for (ADR-0020).

A fixed list, not a free-form base URL. Every run sends the user's key to the
preset's URL from *our* server, so a user-supplied URL would let anyone make
the API call an address of their choosing: an internal service, a cloud
metadata endpoint, a port scan (server-side request forgery). With presets the
destinations are known in advance, and they all speak HTTPS to a public API.

Most presets speak the OpenAI chat-completions dialect, so one client covers
them and only the URL changes. Anthropic gets its native client: its
OpenAI-compatibility layer is documented as meant for testing, not production.

``suggested_models`` are hints for the model field, not an allowlist: model
names change faster than this file, and saving a key checks the chosen model
with one real call anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

PresetKind = Literal["openai_compatible", "anthropic"]
StructuredMethod = Literal["json_schema", "function_calling"]

# Model ids across these providers: letters, digits, and . _ : / @ -
# (OpenRouter uses "vendor/model:variant"). Bounded, so a model name cannot
# smuggle anything into a log line or a URL path.
MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$"
_MODEL_RE = re.compile(MODEL_PATTERN)


@dataclass(frozen=True, slots=True)
class LLMPreset:
    id: str
    label: str
    kind: PresetKind
    base_url: str
    default_model: str
    suggested_models: tuple[str, ...]
    # Where a user creates a key. Shown as a link in the settings form.
    key_url: str
    # How the routing step asks for structured output. ``json_schema`` is
    # OpenAI's strict mode; most compatible APIs either lack it or support it
    # only for some models, while tool calling is nearly universal.
    structured_method: StructuredMethod = "function_calling"


PRESETS: dict[str, LLMPreset] = {
    preset.id: preset
    for preset in (
        LLMPreset(
            id="openai",
            label="OpenAI",
            kind="openai_compatible",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4.1-mini",
            suggested_models=("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o"),
            key_url="https://platform.openai.com/api-keys",
            structured_method="json_schema",
        ),
        LLMPreset(
            id="anthropic",
            label="Anthropic",
            kind="anthropic",
            base_url="https://api.anthropic.com",
            default_model="claude-sonnet-5-5",
            suggested_models=("claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"),
            key_url="https://console.anthropic.com/settings/keys",
        ),
        LLMPreset(
            id="google",
            label="Google Gemini",
            kind="openai_compatible",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_model="gemini-2.5-flash",
            suggested_models=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"),
            key_url="https://aistudio.google.com/apikey",
        ),
        LLMPreset(
            id="groq",
            label="Groq",
            kind="openai_compatible",
            base_url="https://api.groq.com/openai/v1",
            default_model="llama-3.3-70b-versatile",
            suggested_models=(
                "llama-3.3-70b-versatile",
                "openai/gpt-oss-120b",
                "llama-3.1-8b-instant",
            ),
            key_url="https://console.groq.com/keys",
        ),
        LLMPreset(
            id="openrouter",
            label="OpenRouter",
            kind="openai_compatible",
            base_url="https://openrouter.ai/api/v1",
            default_model="openai/gpt-4o-mini",
            suggested_models=(
                "openai/gpt-4o-mini",
                "anthropic/claude-sonnet-4.5",
                "google/gemini-2.5-flash",
                "meta-llama/llama-3.3-70b-instruct",
            ),
            key_url="https://openrouter.ai/keys",
        ),
        LLMPreset(
            id="mistral",
            label="Mistral",
            kind="openai_compatible",
            base_url="https://api.mistral.ai/v1",
            default_model="mistral-small-latest",
            suggested_models=(
                "mistral-small-latest",
                "mistral-medium-latest",
                "mistral-large-latest",
            ),
            key_url="https://console.mistral.ai/api-keys",
        ),
        LLMPreset(
            id="deepseek",
            label="DeepSeek",
            kind="openai_compatible",
            base_url="https://api.deepseek.com/v1",
            default_model="deepseek-chat",
            suggested_models=("deepseek-chat",),
            key_url="https://platform.deepseek.com/api_keys",
        ),
        LLMPreset(
            id="xai",
            label="xAI",
            kind="openai_compatible",
            base_url="https://api.x.ai/v1",
            default_model="grok-3",
            suggested_models=("grok-3", "grok-4"),
            key_url="https://console.x.ai",
        ),
    )
}


def get_preset(preset_id: str) -> LLMPreset | None:
    return PRESETS.get(preset_id)


def is_valid_model_name(model: str) -> bool:
    return _MODEL_RE.fullmatch(model) is not None
