"""Tests for the startup investigation tool-calling fail-fast gate.

Covers `resolve_investigation_capability` (pure, also used by /health) and
`validate_investigation_tooling` (the lifespan gate that refuses to boot on a
tool-incapable investigation model unless ALLOW_TOOLLESS_INVESTIGATION is set).
LLM-critical startup code (CLAUDE.md: NO CODE MERGES WITHOUT TESTS).
"""

from types import SimpleNamespace as NS

import pytest

from faultmaven.config.investigation_capability import (
    InvestigationToolingError,
    resolve_investigation_capability,
    validate_investigation_tooling,
)


class _Provider:
    def __init__(self, tool_capable):
        self._cap = tool_capable

    def supports_tool_calling(self, model=None):
        return self._cap


class _Registry:
    """get_provider returns the configured provider (or None)."""

    def __init__(self, provider):
        self._provider = provider

    def get_provider(self, name):
        return self._provider


class _RaisingRegistry:
    def get_provider(self, name):
        raise RuntimeError("registry not ready")


def _settings(
    provider_name="openai", model="gpt-5.4-mini", *, da_set=False, allow=False
):
    prov = NS(value=provider_name)
    llm = NS(
        da_provider=(prov if da_set else None),
        allow_toolless_investigation=allow,
        get_da_provider=lambda: prov,
        get_da_model=lambda: model,
    )
    return NS(llm=llm)


# --- resolve_investigation_capability (pure) ---------------------------------


def test_resolve_capable():
    cap = resolve_investigation_capability(_settings(), _Registry(_Provider(True)))
    assert cap.tool_capable is True
    assert cap.provider == "openai"
    assert cap.model == "gpt-5.4-mini"
    assert cap.reason is None


def test_resolve_not_capable_sets_reason():
    cap = resolve_investigation_capability(
        _settings("fireworks", "minimax-m3"), _Registry(_Provider(False))
    )
    assert cap.tool_capable is False
    assert "does not support tool calling" in cap.reason
    assert "minimax-m3" in cap.reason


def test_resolve_missing_provider_is_not_capable():
    cap = resolve_investigation_capability(_settings(), _Registry(None))
    assert cap.tool_capable is False
    assert "not initialized" in cap.reason


def test_resolve_registry_error_is_not_capable():
    cap = resolve_investigation_capability(_settings(), _RaisingRegistry())
    assert cap.tool_capable is False
    assert "could not resolve" in cap.reason


def test_resolve_source_chat_vs_da_override():
    chat = resolve_investigation_capability(
        _settings(da_set=False), _Registry(_Provider(True))
    )
    assert chat.source.startswith("CHAT_PROVIDER")
    da = resolve_investigation_capability(
        _settings(da_set=True), _Registry(_Provider(True))
    )
    assert da.source == "DA_PROVIDER"


# --- validate_investigation_tooling (the gate) -------------------------------


def test_validate_capable_passes():
    # No raise.
    validate_investigation_tooling(_settings(), _Registry(_Provider(True)))


def test_validate_toolless_without_optin_raises():
    with pytest.raises(InvestigationToolingError) as exc:
        validate_investigation_tooling(
            _settings("fireworks", "minimax-m3"), _Registry(_Provider(False))
        )
    msg = str(exc.value)
    assert "does not support tool calling" in msg
    # Message must be actionable: both remedies named.
    assert "DA_PROVIDER" in msg
    assert "ALLOW_TOOLLESS_INVESTIGATION" in msg


def test_validate_toolless_with_optin_boots_degraded(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        # No raise — knowing opt-in.
        validate_investigation_tooling(
            _settings("local", "llama3.2", allow=True), _Registry(_Provider(False))
        )
    assert any("DEGRADED" in r.message for r in caplog.records)


def test_validate_missing_provider_without_optin_raises():
    with pytest.raises(InvestigationToolingError):
        validate_investigation_tooling(_settings(), _Registry(None))


def test_validate_missing_provider_with_optin_boots():
    # Can't verify capability, but operator opted in — don't block.
    validate_investigation_tooling(_settings(allow=True), _Registry(None))
