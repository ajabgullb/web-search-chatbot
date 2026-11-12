"""The fixed list of providers users may bring keys for (ADR-0020)."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from app.providers.llm_catalog import PRESETS, get_preset, is_valid_model_name


@pytest.mark.parametrize("preset", PRESETS.values(), ids=lambda preset: preset.id)
def test_every_preset_is_a_public_https_api(preset: object) -> None:
    """Keys go only to these URLs, so each must be TLS to a public hostname."""
    from app.providers.llm_catalog import LLMPreset

    assert isinstance(preset, LLMPreset)
    url = urlsplit(preset.base_url)
    assert url.scheme == "https"
    assert url.hostname and "." in url.hostname
    assert not url.hostname.startswith(("localhost", "127.", "10.", "192.168.", "169.254."))
    assert urlsplit(preset.key_url).scheme == "https"


@pytest.mark.parametrize("preset", PRESETS.values(), ids=lambda preset: preset.id)
def test_every_preset_suggests_valid_models_including_its_default(preset: object) -> None:
    from app.providers.llm_catalog import LLMPreset

    assert isinstance(preset, LLMPreset)
    assert preset.default_model in preset.suggested_models
    assert all(is_valid_model_name(model) for model in preset.suggested_models)


def test_presets_are_keyed_by_their_own_id() -> None:
    assert all(key == preset.id for key, preset in PRESETS.items())
    assert get_preset("openai") is PRESETS["openai"]
    assert get_preset("http://example.com") is None


@pytest.mark.parametrize(
    "model", ["gpt-4.1-mini", "openai/gpt-4o-mini", "meta-llama/llama-3.3-70b:free", "a"]
)
def test_real_model_names_are_accepted(model: str) -> None:
    assert is_valid_model_name(model)


@pytest.mark.parametrize(
    "model", ["", "gpt 4", "-flag", "model\nnext-line", "../x", "x" * 129, "naïve"]
)
def test_odd_model_names_are_refused(model: str) -> None:
    assert not is_valid_model_name(model)
