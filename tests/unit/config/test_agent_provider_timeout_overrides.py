"""AgentSettings.timeout_for_provider — per-provider agent-level overrides.

The agent-level (turn-wide) timeout wraps the entire process_turn call in
``modules/case/api/routes/conversation.py``; provider speed varies enough that a single
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
from pydantic import ValidationError

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
            overrides={"fireworks": 300, "ollama": 600},
        )
        assert s.timeout_for_provider("fireworks") == 300
        assert s.timeout_for_provider("ollama") == 600

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


@pytest.mark.unit
class TestEveryOverrideIsBoundedLikeTheGlobalTimeout:
    """Each override is held to ``agent_request_timeout``'s 30-600 s (#1905).

    The resolved ceiling is published to clients, so an override must not be a
    way past the longest turn a client is told to wait for; an out-of-range
    value refuses the boot, as an out-of-range global value does.
    """

    @pytest.mark.parametrize("seconds", [30, 600])
    def test_the_bounds_are_inclusive(self, monkeypatch, seconds):
        s = _mk_settings(
            monkeypatch, agent_request_timeout=120, overrides={"ollama": seconds}
        )
        assert s.timeout_for_provider("ollama") == seconds

    @pytest.mark.parametrize("seconds", [29, 601, 900, 0, -1])
    def test_an_override_outside_the_bounds_refuses_to_load(self, monkeypatch, seconds):
        with pytest.raises(ValidationError) as refused:
            _mk_settings(
                monkeypatch, agent_request_timeout=120, overrides={"ollama": seconds}
            )
        message = str(refused.value)
        assert "AGENT_PROVIDER_TIMEOUT_OVERRIDES" in message
        assert f"ollama={seconds}" in message
        assert "30-600 seconds" in message

    def test_the_message_names_every_offending_provider_and_only_those(
        self, monkeypatch
    ):
        with pytest.raises(ValidationError) as refused:
            _mk_settings(
                monkeypatch,
                agent_request_timeout=120,
                overrides={"ollama": 900, "groq": 29, "gemini": 300},
            )
        message = str(refused.value)
        assert "ollama=900" in message
        assert "groq=29" in message
        assert "gemini" not in message.split("out of range:")[1].split("[")[0]

    def test_a_non_integer_override_refuses_to_load(self, monkeypatch):
        with pytest.raises(ValidationError) as refused:
            _mk_settings(
                monkeypatch, agent_request_timeout=120, overrides={"ollama": "slow"}
            )
        assert "ollama" in str(refused.value)

    def test_the_global_timeout_and_the_overrides_share_one_pair_of_bounds(self):
        """A bound moved on one field and not the other is what #1905 closed."""
        from faultmaven.config.settings import (
            MAX_AGENT_TIMEOUT_SECONDS,
            MIN_AGENT_TIMEOUT_SECONDS,
        )

        metadata = AgentSettings.model_fields["agent_request_timeout"].metadata
        ge = next(m.ge for m in metadata if hasattr(m, "ge"))
        le = next(m.le for m in metadata if hasattr(m, "le"))
        assert (ge, le) == (MIN_AGENT_TIMEOUT_SECONDS, MAX_AGENT_TIMEOUT_SECONDS)
        assert (MIN_AGENT_TIMEOUT_SECONDS, MAX_AGENT_TIMEOUT_SECONDS) == (30, 600)
