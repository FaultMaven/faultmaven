"""``provider_timeout``: one provider's request timeout ``T`` (#2983).

Some providers / models are systematically slower than others (e.g.
Fireworks DeepSeek V4 Pro on schema-forced tool-loop iterations, local
Ollama on CPU). ``LLM_PROVIDER_TIMEOUT_OVERRIDES`` gives one its own ``T``.

The rule, the one resolver's: an explicit override is that provider's ``T``,
below the base as well as above it; otherwise the base, ``LLM_REQUEST_TIMEOUT``.
An override ``<= 0`` is refused when the settings load.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from faultmaven.config.settings import LLMSettings
from faultmaven.infrastructure.llm.provider_timeout import provider_timeout


def _mk_settings(
    monkeypatch, *, request_timeout: int, overrides: dict | None
) -> SimpleNamespace:
    """Build LLMSettings from the environment, its one source, as at boot."""
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", str(request_timeout))
    if overrides is not None:
        monkeypatch.setenv("LLM_PROVIDER_TIMEOUT_OVERRIDES", json.dumps(overrides))
    else:
        monkeypatch.delenv("LLM_PROVIDER_TIMEOUT_OVERRIDES", raising=False)
    return SimpleNamespace(llm=LLMSettings())


@pytest.mark.unit
class TestProviderTimeout:
    def test_empty_overrides_falls_back_to_base(self, monkeypatch):
        """No overrides → all providers get ``request_timeout``."""
        s = _mk_settings(monkeypatch, request_timeout=30, overrides={})
        assert provider_timeout("fireworks", s) == 30
        assert provider_timeout("gemini", s) == 30

    def test_named_override_returned_for_match(self, monkeypatch):
        """A configured override wins over the base timeout."""
        s = _mk_settings(
            monkeypatch,
            request_timeout=30,
            overrides={"fireworks": 180, "local": 600},
        )
        assert provider_timeout("fireworks", s) == 180
        assert provider_timeout("local", s) == 600

    def test_an_override_below_the_base_lowers_it(self, monkeypatch):
        """An override is that provider's ``T`` outright, not ``max`` with the
        base. Mutant: ``max(override, base)``."""
        s = _mk_settings(monkeypatch, request_timeout=30, overrides={"groq": 10})
        assert provider_timeout("groq", s) == 10
        assert provider_timeout("gemini", s) == 30

    def test_unknown_provider_falls_back_to_base(self, monkeypatch):
        """Providers not listed in overrides take ``request_timeout``."""
        s = _mk_settings(
            monkeypatch,
            request_timeout=30,
            overrides={"fireworks": 180},
        )
        assert provider_timeout("anthropic", s) == 30
        assert provider_timeout("openai", s) == 30

    def test_none_provider_returns_base(self, monkeypatch):
        """Defensive: None and empty string don't crash; both return base."""
        s = _mk_settings(monkeypatch, request_timeout=45, overrides={"gemini": 120})
        assert provider_timeout(None, s) == 45
        assert provider_timeout("", s) == 45

    @pytest.mark.parametrize("seconds", [0, -5])
    def test_an_override_at_or_below_zero_is_refused_at_load(
        self, monkeypatch, seconds
    ):
        """``call_external`` reads ``0`` as unbounded and a negative value as
        an instant timeout, so neither is a usable ``T``."""
        with pytest.raises(ValidationError, match=f"refused: groq={seconds}"):
            _mk_settings(monkeypatch, request_timeout=30, overrides={"groq": seconds})

    @pytest.mark.parametrize("seconds", [0, -5])
    def test_a_base_at_or_below_zero_is_refused_at_load(self, monkeypatch, seconds):
        """``LLM_REQUEST_TIMEOUT`` is every provider's ``T`` without an
        override, so it is held to the same bound."""
        with pytest.raises(ValidationError, match="greater than 0"):
            _mk_settings(monkeypatch, request_timeout=seconds, overrides={})
