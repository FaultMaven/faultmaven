"""AgentSettings.timeout_for_provider — per-provider agent-level overrides.

The agent-level (turn-wide) timeout wraps the entire process_turn call in
``modules/case/api/routes.py``; provider speed varies enough that a single
global ceiling either fails slow providers (Fireworks DeepSeek V4 Pro on
log-heavy cases, local Ollama on CPU) or wastes headroom on faster ones.

This pins the contract that ``AgentSettings.timeout_for_provider`` returns
the per-provider override when one is set and falls back to
``agent_request_timeout`` otherwise. Mirrors the pattern from
test_provider_timeout_overrides.py (LLM-router level, ISS-054).

Surfaced by ISS-058 — DeepSeek run on logs-windows q3 hit the 120s
agent ceiling.
"""

from __future__ import annotations

import json

import pytest

from faultmaven.config.settings import AgentSettings


def _mk_settings(
    monkeypatch, *, agent_request_timeout: int, overrides: dict | None
) -> AgentSettings:
    """Build AgentSettings via env so pydantic-settings reads our values."""
    monkeypatch.setenv("AGENT_REQUEST_TIMEOUT", str(agent_request_timeout))
    if overrides is not None:
        monkeypatch.setenv("AGENT_PROVIDER_TIMEOUT_OVERRIDES", json.dumps(overrides))
    else:
        monkeypatch.delenv("AGENT_PROVIDER_TIMEOUT_OVERRIDES", raising=False)
    monkeypatch.setattr(
        AgentSettings,
        "model_config",
        {**AgentSettings.model_config, "env_file": "/dev/null"},
    )
    return AgentSettings()


@pytest.mark.unit
class TestAgentProviderTimeoutOverrides:
    def test_empty_overrides_falls_back_to_base(self, monkeypatch):
        """No overrides → all providers get ``agent_request_timeout``."""
        s = _mk_settings(monkeypatch, agent_request_timeout=120, overrides={})
        assert s.timeout_for_provider("fireworks") == 120
        assert s.timeout_for_provider("gemini") == 120

    def test_named_override_returned_for_match(self, monkeypatch):
        """A configured override wins over the base agent timeout."""
        s = _mk_settings(
            monkeypatch,
            agent_request_timeout=120,
            overrides={"fireworks": 300, "ollama": 900},
        )
        assert s.timeout_for_provider("fireworks") == 300
        assert s.timeout_for_provider("ollama") == 900

    def test_unknown_provider_falls_back_to_base(self, monkeypatch):
        """Providers not in overrides take ``agent_request_timeout``."""
        s = _mk_settings(
            monkeypatch,
            agent_request_timeout=120,
            overrides={"fireworks": 300},
        )
        assert s.timeout_for_provider("anthropic") == 120
        assert s.timeout_for_provider("openai") == 120

    def test_none_provider_returns_base(self, monkeypatch):
        """None and empty string don't crash; both return base."""
        s = _mk_settings(
            monkeypatch, agent_request_timeout=180, overrides={"gemini": 240}
        )
        assert s.timeout_for_provider(None) == 180
        assert s.timeout_for_provider("") == 180
