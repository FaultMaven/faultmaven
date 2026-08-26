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


# --- Axis 3 (advisory): warn_best_effort_enforcement --------------------------
#
# Warning-only: best-effort chat/investigation is supported (demo/eval), but
# must be VISIBLE at boot rather than discovered from silently degraded
# investigations. Never blocks, never raises; classifier/synthesis exempt.

import logging as _logging

from faultmaven.config.investigation_capability import (
    resolve_enforcement_classes,
    warn_best_effort_enforcement,
)


class _EnforcementProvider:
    def __init__(self, capability_value):
        self._value = capability_value

    def get_structured_output_capability(self, model=None):
        return NS(value=self._value)


class _PerProviderRegistry:
    """get_provider by name — lets chat and structured-output differ."""

    def __init__(self, providers):
        self._providers = providers

    def get_provider(self, name):
        return self._providers.get(name)


def _enforcement_settings(
    provider_name="fireworks",
    model="deepseek-v4-flash",
    *,
    da_set=False,
    da_provider_name=None,
    da_model=None,
    so_provider_name=None,
    so_model="gpt-5.4-mini",
):
    prov = NS(value=provider_name)
    da_prov = NS(value=da_provider_name) if da_provider_name else prov
    so_prov = NS(value=so_provider_name) if so_provider_name else None
    llm = NS(
        provider=prov,
        # The BASE {PROVIDER}_MODEL — what the registry builds the provider
        # with, and what the chat row is judged by. Real LLMSettings always
        # carries this field per provider.
        **{f"{provider_name}_model": model},
        da_provider=(da_prov if da_set else None),
        structured_output_provider=so_prov,
        get_da_provider=lambda: (da_prov if da_set else prov),
        get_da_model=lambda: (da_model if da_set and da_model else model),
        get_model=lambda task="chat": model,
        get_structured_output_provider=lambda: so_prov,
        get_structured_output_model=lambda: so_model,
    )
    return NS(llm=llm)


def test_best_effort_chat_warns_once_for_coinciding_roles(caplog):
    """DA unset → investigation and chat resolve identically → ONE warning
    naming both, not two."""
    settings = _enforcement_settings()
    registry = _PerProviderRegistry({"fireworks": _EnforcementProvider("best_effort")})
    with caplog.at_level(_logging.WARNING):
        warn_best_effort_enforcement(settings, registry)
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert len(warnings) == 1
    assert "investigation/chat" in warnings[0].message
    assert "BEST_EFFORT" in warnings[0].message


def test_enforced_models_stay_silent(caplog):
    settings = _enforcement_settings(provider_name="openai", model="gpt-5.4-mini")
    registry = _PerProviderRegistry({"openai": _EnforcementProvider("strict")})
    with caplog.at_level(_logging.INFO):
        warn_best_effort_enforcement(settings, registry)
    assert not [r for r in caplog.records if r.levelno >= _logging.INFO]


def test_function_calling_counts_as_enforced(caplog):
    settings = _enforcement_settings(provider_name="anthropic", model="claude-x")
    registry = _PerProviderRegistry(
        {"anthropic": _EnforcementProvider("function_calling")}
    )
    with caplog.at_level(_logging.WARNING):
        warn_best_effort_enforcement(settings, registry)
    assert not caplog.records


def test_da_override_checks_both_roles_separately(caplog):
    """DA on a best-effort provider + chat on STRICT → exactly one warning,
    for the investigation role only."""
    settings = _enforcement_settings(
        provider_name="openai",
        model="gpt-5.4-mini",
        da_set=True,
        da_provider_name="fireworks",
        da_model="deepseek-v4-flash",
    )
    registry = _PerProviderRegistry(
        {
            "openai": _EnforcementProvider("strict"),
            "fireworks": _EnforcementProvider("best_effort"),
        }
    )
    with caplog.at_level(_logging.WARNING):
        warn_best_effort_enforcement(settings, registry)
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert len(warnings) == 1
    assert "investigation (DA→CHAT)" in warnings[0].message
    assert "fireworks" in warnings[0].message


def test_structured_output_override_downgrades_to_info(caplog):
    """An explicitly-set, ENFORCED structured-output route compensates: INFO,
    not WARNING (the escape hatch the warning itself recommends)."""
    settings = _enforcement_settings(so_provider_name="openai")
    registry = _PerProviderRegistry(
        {
            "fireworks": _EnforcementProvider("best_effort"),
            "openai": _EnforcementProvider("strict"),
        }
    )
    with caplog.at_level(_logging.INFO):
        warn_best_effort_enforcement(settings, registry)
    assert not [r for r in caplog.records if r.levelno == _logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == _logging.INFO]
    assert len(infos) == 1
    assert "STRUCTURED_OUTPUT_PROVIDER" in infos[0].message


def test_best_effort_structured_override_does_not_compensate(caplog):
    settings = _enforcement_settings(so_provider_name="groq", so_model="llama-x")
    registry = _PerProviderRegistry(
        {
            "fireworks": _EnforcementProvider("best_effort"),
            "groq": _EnforcementProvider("best_effort"),
        }
    )
    with caplog.at_level(_logging.WARNING):
        warn_best_effort_enforcement(settings, registry)
    assert [r for r in caplog.records if r.levelno == _logging.WARNING]


def test_unknown_capability_fails_open_silently(caplog):
    """No provider / no probe / raising probe → no verdict, no warning (same
    fail-open-on-ignorance philosophy as axis 2)."""
    settings = _enforcement_settings()
    with caplog.at_level(_logging.WARNING):
        warn_best_effort_enforcement(settings, _PerProviderRegistry({}))
        warn_best_effort_enforcement(settings, _RaisingRegistry())
        warn_best_effort_enforcement(
            settings, _PerProviderRegistry({"fireworks": _Provider(True)})
        )
    assert not caplog.records


def test_never_raises_on_malformed_settings():
    warn_best_effort_enforcement(NS(llm=NS()), _PerProviderRegistry({}))


class _ByModelEnforcementProvider:
    """Capability that depends on the MODEL, not just the provider — the only
    way to tell "judged by the base model" from "judged by the per-task chat
    model" apart."""

    def __init__(self, by_model, default):
        self._by_model = by_model
        self._default = default

    def get_structured_output_capability(self, model=None):
        return NS(value=self._by_model.get(model, self._default))


def test_pinned_da_provider_is_not_compensated_by_structured_output_override(caplog):
    """A pinned DA_PROVIDER takes precedence over STRUCTURED_OUTPUT_PROVIDER on
    the tool path (``milestone_engine`` applies the override only when no DA
    provider is set), so a strict override does NOT carry a best-effort DA
    provider's schema-bound calls. Reporting that as "acceptable" would clear
    exactly the misconfiguration this check exists to catch."""
    settings = _enforcement_settings(
        provider_name="openai",
        model="gpt-5.4-mini",
        da_set=True,
        da_provider_name="fireworks",
        da_model="deepseek-v4-flash",
        so_provider_name="openai",
    )
    registry = _PerProviderRegistry(
        {
            "openai": _EnforcementProvider("strict"),
            "fireworks": _EnforcementProvider("best_effort"),
        }
    )
    with caplog.at_level(_logging.INFO):
        warn_best_effort_enforcement(settings, registry)

    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert len(warnings) == 1
    assert "investigation (DA→CHAT)" in warnings[0].message
    # Never the "acceptable" line — that is the false clear.
    assert not [r for r in caplog.records if r.levelno == _logging.INFO]


def test_unpinned_da_is_still_compensated(caplog):
    """The compensation is narrowed, not removed: with DA unset the engine does
    apply the override, so an enforced route still downgrades to INFO."""
    settings = _enforcement_settings(so_provider_name="openai")
    registry = _PerProviderRegistry(
        {
            "fireworks": _EnforcementProvider("best_effort"),
            "openai": _EnforcementProvider("strict"),
        }
    )
    resolved = resolve_enforcement_classes(settings, registry)
    assert resolved.structured_output_enforced is True
    assert all(role.compensated for role in resolved.roles)
    assert resolved.degraded_roles == ()


def test_chat_row_judged_by_base_model_not_the_dead_chat_task_model():
    """``{PROVIDER}_CHAT_MODEL`` is dead config surface — nothing at runtime
    resolves it (the registry builds the provider with the BASE model) — so a
    STRICT value there must not clear a BEST_EFFORT base model that the engine
    actually sends."""
    settings = _enforcement_settings(
        provider_name="fireworks", model="deepseek-v4-flash"
    )
    # A per-task "chat" model the check must ignore, reporting the opposite class.
    settings.llm.get_model = lambda task="chat": "gpt-5.4-mini"
    registry = _PerProviderRegistry(
        {
            "fireworks": _ByModelEnforcementProvider(
                {"deepseek-v4-flash": "best_effort", "gpt-5.4-mini": "strict"},
                "strict",
            )
        }
    )

    resolved = resolve_enforcement_classes(settings, registry)

    assert [role.model for role in resolved.roles] == ["deepseek-v4-flash"]
    assert [role.role for role in resolved.roles] == ["investigation/chat"]
    assert resolved.degraded_roles == resolved.roles


def test_resolver_reports_the_verdict_as_data():
    """Axis 3 is readable as a value, not only as a boot log line — a boot log
    is gone from ``kubectl logs`` long before someone debugs the degradation."""
    settings = _enforcement_settings()
    registry = _PerProviderRegistry({"fireworks": _EnforcementProvider("best_effort")})

    resolved = resolve_enforcement_classes(settings, registry)

    assert len(resolved.roles) == 1
    role = resolved.roles[0]
    assert (role.role, role.provider, role.model) == (
        "investigation/chat",
        "fireworks",
        "deepseek-v4-flash",
    )
    assert role.enforcement == "best_effort"
    assert role.unenforced and role.degraded
    assert resolved.structured_output_enforced is False


def test_resolver_never_raises_and_reports_nothing_on_malformed_settings():
    resolved = resolve_enforcement_classes(NS(llm=NS()), _PerProviderRegistry({}))
    assert resolved.roles == ()
    assert resolved.degraded_roles == ()
