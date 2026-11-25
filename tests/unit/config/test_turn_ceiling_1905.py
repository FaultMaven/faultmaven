"""``resolve_turn_ceiling``: the one owner of the turn's ceiling and response bound (#1905).

Every reader of the turn's timing reads these two numbers from here — the turn
route (its deadline and its in-flight claim), ``GET /api/v1/meta/capabilities``,
``GET /admin/config/status`` and the retry-ladder report — so a second copy of
the arithmetic cannot drift from the one clients are told.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from faultmaven.config.settings import AgentSettings, LLMProvider
from faultmaven.config.turn_ceiling import resolve_turn_ceiling
from faultmaven.core.investigation.turn_budget import (
    AUTO_TITLE_TIMEOUT_SECONDS,
    TURN_COMMIT_RESERVE_SECONDS,
)

pytestmark = pytest.mark.unit


def _agent(monkeypatch, *, base: int, overrides: dict) -> AgentSettings:
    """A REAL ``AgentSettings``, loaded from the environment as at boot."""
    monkeypatch.setenv("AGENT_REQUEST_TIMEOUT", str(base))
    monkeypatch.setenv("AGENT_PROVIDER_TIMEOUT_OVERRIDES", json.dumps(overrides))
    monkeypatch.setattr(
        AgentSettings,
        "model_config",
        {**AgentSettings.model_config, "env_file": "/dev/null"},
    )
    return AgentSettings()


def _settings(monkeypatch, provider, *, base=120, overrides=None):
    return SimpleNamespace(
        llm=SimpleNamespace(provider=provider),
        agent=_agent(monkeypatch, base=base, overrides=overrides or {}),
    )


class TestTheCeiling:
    def test_the_chat_providers_override_applies(self, monkeypatch):
        settings = _settings(
            monkeypatch, LLMProvider.GROQ, overrides={"groq": 300, "gemini": 200}
        )

        ceiling = resolve_turn_ceiling(settings)

        assert ceiling.provider == "groq"
        assert ceiling.ceiling_seconds == 300.0

    def test_a_fallback_providers_override_does_not(self, monkeypatch):
        """Only the chat provider's entry applies: a fallback the router reaches
        mid-turn runs inside the deadline already bound, so its own (larger)
        override never lengthens the turn."""
        settings = _settings(
            monkeypatch, LLMProvider.ANTHROPIC, overrides={"gemini": 600}
        )

        assert resolve_turn_ceiling(settings).ceiling_seconds == 120.0

    def test_a_provider_switch_changes_it(self, monkeypatch):
        """The dashboard override writes ``settings.llm.provider`` in place
        (``llm_config_overrides``); the next resolution must see it."""
        settings = _settings(
            monkeypatch, LLMProvider.GROQ, overrides={"groq": 300, "gemini": 200}
        )
        before = resolve_turn_ceiling(settings)

        settings.llm.provider = LLMProvider.GEMINI
        after = resolve_turn_ceiling(settings)

        assert (before.ceiling_seconds, after.ceiling_seconds) == (300.0, 200.0)
        assert after.provider == "gemini"

    def test_no_provider_anywhere_takes_the_global_ceiling(self, monkeypatch):
        monkeypatch.delenv("CHAT_PROVIDER", raising=False)
        settings = _settings(monkeypatch, None, base=180, overrides={"groq": 300})

        ceiling = resolve_turn_ceiling(settings)

        assert (ceiling.provider, ceiling.ceiling_seconds) == (None, 180.0)


class TestTheResponseBound:
    def test_it_is_the_ceiling_plus_the_commit_and_the_auto_title(self, monkeypatch):
        settings = _settings(monkeypatch, LLMProvider.GROQ, overrides={"groq": 300})

        ceiling = resolve_turn_ceiling(settings)

        assert ceiling.response_bound_seconds == (
            300.0 + TURN_COMMIT_RESERVE_SECONDS + AUTO_TITLE_TIMEOUT_SECONDS
        )

    def test_it_moves_with_the_provider(self, monkeypatch):
        settings = _settings(monkeypatch, LLMProvider.GROQ, overrides={"groq": 300})
        groq = resolve_turn_ceiling(settings).response_bound_seconds

        settings.llm.provider = LLMProvider.OPENAI
        openai = resolve_turn_ceiling(settings).response_bound_seconds

        assert groq - openai == 300.0 - 120.0


class TestTheOtherReadersReadIt:
    def test_the_retry_ladder_report_budgets_against_the_same_ceiling(
        self, monkeypatch
    ):
        """``describe_retry_ladder_budget`` once looked the agent timeout up
        itself; it now reads the resolver, so a report and the turn cannot
        disagree about the turn's budget."""
        from faultmaven.config import retry_budget

        seen = {}

        def _ceiling(settings):
            seen["called"] = True
            return SimpleNamespace(ceiling_seconds=432.0)

        monkeypatch.setattr(
            "faultmaven.config.turn_ceiling.resolve_turn_ceiling", _ceiling
        )
        monkeypatch.setattr(
            "faultmaven.core.investigation.turn_budget.worst_case_ladder_plan",
            lambda **kwargs: seen.setdefault("agent_timeout", kwargs["agent_timeout"]),
        )
        settings = SimpleNamespace(
            llm=SimpleNamespace(
                provider="groq",
                request_timeout=30,
                timeout_for_provider=lambda _name: 30,
            ),
            agent=SimpleNamespace(timeout_for_provider=lambda _name: 120),
        )
        monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)

        retry_budget.describe_retry_ladder_budget(settings)

        assert seen == {"called": True, "agent_timeout": 432.0}

    def test_the_claim_ttl_is_the_response_bound_plus_its_margin(self, monkeypatch):
        """The in-flight claim's TTL reads the bound rather than re-adding the
        commit reserve and the auto-title itself (#1888's formula, #1905)."""
        import math

        from faultmaven.modules.case.api import turn_idempotency

        settings = _settings(monkeypatch, LLMProvider.GROQ, overrides={"groq": 300})
        bound = resolve_turn_ceiling(settings).response_bound_seconds

        assert turn_idempotency.claim_ttl_seconds(bound) == math.ceil(
            bound + turn_idempotency.CLAIM_MARGIN_SECONDS
        )
