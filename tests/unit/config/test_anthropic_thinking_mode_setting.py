"""ANTHROPIC_THINKING_MODE must never be able to down the API (#1116).

pydantic-settings' case-insensitivity applies to env var NAMES, not values, so
a ``Literal`` field would raise a ValidationError — aborting settings
construction and refusing to boot — for ``ANTHROPIC_THINKING_MODE=OFF``: an
operator trying to turn a default-off experiment knob OFF. The field is a
normalizing ``str`` instead.

Pins:
  1. case is normalized ("OFF" → "off", "Adaptive" → "adaptive");
  2. surrounding whitespace is stripped (" adaptive " → "adaptive");
  3. an unrecognized value FAILS CLOSED to "off" AND logs a WARNING —
     a silent fallback would leave an operator believing thinking is on;
  4. valid values pass through unchanged, and the default is "off".
"""

import logging

import pytest

from faultmaven.config.settings import ANTHROPIC_THINKING_MODES, LLMSettings


@pytest.mark.unit
class TestAnthropicThinkingModeNormalization:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_THINKING_MODE", raising=False)
        assert LLMSettings().anthropic_thinking_mode == "off"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("OFF", "off"),
            ("Off", "off"),
            ("ADAPTIVE", "adaptive"),
            ("Adaptive", "adaptive"),
            ("ENABLED", "enabled"),
        ],
    )
    def test_uppercase_values_are_normalized_not_rejected(
        self, raw, expected, monkeypatch
    ):
        """The near-miss that matters most: an operator turning it OFF."""
        monkeypatch.setenv("ANTHROPIC_THINKING_MODE", raw)
        assert LLMSettings().anthropic_thinking_mode == expected

    @pytest.mark.parametrize("raw", [" adaptive ", "\tadaptive", "adaptive\n"])
    def test_surrounding_whitespace_is_stripped(self, raw, monkeypatch):
        """Whitespace can survive dotenv parsing — it must not be fatal."""
        monkeypatch.setenv("ANTHROPIC_THINKING_MODE", raw)
        assert LLMSettings().anthropic_thinking_mode == "adaptive"

    @pytest.mark.parametrize("raw", ["adaptiv", "on", "true", "yes", ""])
    def test_unrecognized_value_fails_closed_to_off_with_a_warning(
        self, raw, caplog, monkeypatch
    ):
        """Fail closed AND say so — a silent fallback would leave the operator
        believing thinking is on when it is not."""
        monkeypatch.setenv("ANTHROPIC_THINKING_MODE", raw)
        with caplog.at_level(logging.WARNING, logger="faultmaven.config.settings"):
            settings = LLMSettings()

        assert settings.anthropic_thinking_mode == "off"
        assert any(
            "ANTHROPIC_THINKING_MODE" in r.message for r in caplog.records
        ), "unrecognized value must warn, not fall back silently"

    def test_valid_values_pass_through_unchanged(self, monkeypatch):
        for mode in ANTHROPIC_THINKING_MODES:
            monkeypatch.setenv("ANTHROPIC_THINKING_MODE", mode)
            assert LLMSettings().anthropic_thinking_mode == mode

    def test_constructing_settings_never_raises_on_a_bad_value(self, monkeypatch):
        """The whole point: a typo is a degraded knob, not a downed API."""
        monkeypatch.setenv("ANTHROPIC_THINKING_MODE", "totally-bogus")
        LLMSettings()
