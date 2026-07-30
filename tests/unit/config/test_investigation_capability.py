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
    StructuredOutputCapacityError,
    resolve_investigation_capability,
    resolve_schema_capacity,
    validate_investigation_tooling,
    validate_structured_output_capacity,
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


# --- schema-capacity axis (STRUCTURED_OUTPUT → CHAT) -------------------------
#
# Second, independent gate: can the resolved structured-output model SERVE the
# engine's response schemas? Distinct from tool calling (axis 1) and from
# get_structured_output_capability (which answers *how* a schema is enforced, not
# whether it is accepted at all).


class _SchemaProvider:
    """Provider reporting response-schema capacity; optionally raises."""

    def __init__(self, capable=True, *, raises=False):
        self._capable = capable
        self._raises = raises

    def supports_engine_response_schemas(self, model=None):
        if self._raises:
            raise RuntimeError("probe exploded")
        return self._capable


class _LegacyProvider:
    """Provider with no capacity method at all."""


def _so_settings(provider_name="gemini", model="gemini-3.5-flash", *, so_set=False):
    prov = NS(value=provider_name)
    llm = NS(
        structured_output_provider=(prov if so_set else None),
        get_structured_output_provider=lambda: prov,
        get_structured_output_model=lambda: model,
    )
    return NS(llm=llm)


def test_resolve_capacity_capable():
    cap = resolve_schema_capacity(_so_settings(), _Registry(_SchemaProvider(True)))
    assert cap.can_serve_schemas is True
    assert cap.inconclusive is False
    assert cap.reason is None
    assert cap.model == "gemini-3.5-flash"


def test_resolve_capacity_incapable_names_model_and_source():
    cap = resolve_schema_capacity(
        _so_settings("gemini", "gemini-2.5-flash"),
        _Registry(_SchemaProvider(False)),
    )
    assert cap.can_serve_schemas is False
    assert cap.inconclusive is False
    assert "gemini-2.5-flash" in cap.reason
    assert "CHAT_PROVIDER" in cap.source


def test_resolve_capacity_source_reflects_so_override():
    assert (
        "CHAT_PROVIDER"
        in resolve_schema_capacity(
            _so_settings(so_set=False), _Registry(_SchemaProvider(True))
        ).source
    )
    assert (
        resolve_schema_capacity(
            _so_settings(so_set=True), _Registry(_SchemaProvider(True))
        ).source
        == "STRUCTURED_OUTPUT_PROVIDER"
    )


@pytest.mark.parametrize(
    "registry",
    [
        _Registry(None),  # provider not initialized
        _RaisingRegistry(),  # registry not ready
        _Registry(_LegacyProvider()),  # provider doesn't report capacity
        _Registry(_SchemaProvider(raises=True)),  # probe itself raises
    ],
)
def test_resolve_capacity_unknown_is_inconclusive_not_a_failure(registry):
    """Ignorance must never be read as incapacity — otherwise the gate would
    block boot on every unmeasured provider."""
    cap = resolve_schema_capacity(_so_settings(), registry)
    assert cap.inconclusive is True
    assert cap.can_serve_schemas is True
    assert cap.reason


def test_validate_capacity_capable_passes():
    validate_structured_output_capacity(
        _so_settings(), _Registry(_SchemaProvider(True))
    )


def test_validate_capacity_incapable_refuses_boot_with_remedy():
    with pytest.raises(StructuredOutputCapacityError) as exc:
        validate_structured_output_capacity(
            _so_settings("gemini", "gemini-2.5-flash"),
            _Registry(_SchemaProvider(False)),
        )
    msg = str(exc.value)
    assert "gemini-2.5-flash" in msg  # what is wrong
    assert "gemini-3.5-flash" in msg  # what to do instead
    assert "STRUCTURED_OUTPUT_PROVIDER" in msg  # the other way out


@pytest.mark.parametrize(
    "registry",
    [_Registry(None), _RaisingRegistry(), _Registry(_LegacyProvider())],
)
def test_validate_capacity_fails_open_when_inconclusive(registry):
    """No raise: an unmeasured model boots rather than being presumed broken."""
    validate_structured_output_capacity(_so_settings(), registry)


def test_capacity_gate_has_no_opt_out_flag():
    """Unlike the tooling gate, there is deliberately no bypass: without the
    stage schema the engine cannot record state, so there is no degraded mode to
    opt into. Pinned so a future 'just let it boot' flag is a conscious change."""
    settings = _so_settings("gemini", "gemini-2.5-flash")
    settings.llm.allow_toolless_investigation = True  # unrelated flag, must not help
    with pytest.raises(StructuredOutputCapacityError):
        validate_structured_output_capacity(settings, _Registry(_SchemaProvider(False)))
