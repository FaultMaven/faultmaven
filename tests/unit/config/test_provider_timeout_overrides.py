"""LLMSettings.timeout_for_provider — per-provider timeout overrides.

Some providers / models are systematically slower than others (e.g.
Fireworks DeepSeek V4 Pro on schema-forced tool-loop iterations, local
Ollama on CPU). The 2026-05-01 system code review surfaced a hard
``TimeoutError`` when a Fireworks call exceeded the global 90 s ceiling.

This test pins the contract that ``LLMSettings.timeout_for_provider``
returns the per-provider override when one is set and falls back to
``request_timeout`` otherwise.
"""

from __future__ import annotations

import json

import pytest

from faultmaven.config.settings import LLMSettings


def _mk_settings(
    monkeypatch, *, request_timeout: int, overrides: dict | None
) -> LLMSettings:
    """Build LLMSettings from the environment, its one source, as at boot."""
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", str(request_timeout))
    if overrides is not None:
        monkeypatch.setenv("LLM_PROVIDER_TIMEOUT_OVERRIDES", json.dumps(overrides))
    else:
        monkeypatch.delenv("LLM_PROVIDER_TIMEOUT_OVERRIDES", raising=False)
    return LLMSettings()


@pytest.mark.unit
class TestProviderTimeoutOverrides:
    def test_empty_overrides_falls_back_to_base(self, monkeypatch):
        """No overrides → all providers get ``request_timeout``."""
        s = _mk_settings(monkeypatch, request_timeout=30, overrides={})
        assert s.timeout_for_provider("fireworks") == 30
        assert s.timeout_for_provider("gemini") == 30

    def test_named_override_returned_for_match(self, monkeypatch):
        """A configured override wins over the base timeout."""
        s = _mk_settings(
            monkeypatch,
            request_timeout=30,
            overrides={"fireworks": 180, "ollama": 600},
        )
        assert s.timeout_for_provider("fireworks") == 180
        assert s.timeout_for_provider("ollama") == 600

    def test_unknown_provider_falls_back_to_base(self, monkeypatch):
        """Providers not listed in overrides take ``request_timeout``."""
        s = _mk_settings(
            monkeypatch,
            request_timeout=30,
            overrides={"fireworks": 180},
        )
        assert s.timeout_for_provider("anthropic") == 30
        assert s.timeout_for_provider("openai") == 30

    def test_none_provider_returns_base(self, monkeypatch):
        """Defensive: None and empty string don't crash; both return base."""
        s = _mk_settings(monkeypatch, request_timeout=45, overrides={"gemini": 120})
        assert s.timeout_for_provider(None) == 45
        assert s.timeout_for_provider("") == 45
