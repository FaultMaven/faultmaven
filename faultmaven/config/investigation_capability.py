"""Startup fail-fast gates for investigation model capability.

Two gates, one per axis — see the summary at the end of this docstring. Axis 1
(tool calling) is described first because it is the older and larger of the two.

The investigation engine's Directed Analysis tools (``search_file``,
``deep_analysis``, KB lookups) are how it **gathers evidence**, and they require
the model to support function/tool calling. A tool-incapable investigation model
can't reach the evidence — yet the engine will still emit conclusions from
whatever is already in context. That is exactly the *premature / unfounded
conclusion* FaultMaven guarantees against (inadequate data → keep engaging and
name the gap, never conclude early).

So this gate refuses to boot when the **resolved investigation model**
(``DA_PROVIDER`` → falling back to ``CHAT_PROVIDER``) is tool-incapable —
*unless* the operator explicitly opts in via ``ALLOW_TOOLLESS_INVESTIGATION``
(a knowing choice to run degraded/offline, e.g. a local model — never an
accident). When opted in, the gate logs a loud warning instead of failing; the
``/health`` endpoint then reports ``degraded`` so the state stays visible.

Companion to the per-turn runtime fallback in ``milestone_engine`` (which still
catches transient tool failures on a normally-capable model) and to the
deployment-coherence / LLM-credential gates — all three are explicit, fail-fast
checks called from the lifespan.

This module gates two independent axes, each with its own resolved provider:

1. **Tool calling** on the DA→CHAT investigation model
   (``validate_investigation_tooling``) — can the engine *reach* the evidence.
2. **Response-schema capacity** on the STRUCTURED_OUTPUT→CHAT model
   (``validate_structured_output_capacity``) — can the engine *record what it
   found*. A constrained-decoding backend compiles the response schema into a
   state machine and can reject the engine's larger stage schemas outright
   (Gemini: ``400 ... too many states for serving``). That rejection is
   deterministic per model, so it is a capability, not a transient fault.

Axis 2 is gated because the failure is otherwise both late and total: the DIAGNOSIS
schema is only sent once a case reaches that stage, so an incompatible model
serves several turns of a live investigation and *then* fails every remaining
turn. Boot is the honest place to say "this model cannot run this engine".

What is deliberately NOT gated: whether the structured-output provider supports
*tool calling*. Structured output uses ``json_schema`` / ``response_format``,
which does not require it, so a tool-incapable structured-output provider is not
a problem — that is why axis 1 resolves the DA→CHAT provider and not this one.

The registry is passed in (not imported) so this config-layer module stays free
of an infrastructure import; the gate is skipped in test environments, matching
the other startup service gates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from faultmaven.config.settings import Settings

logger = logging.getLogger(__name__)


class InvestigationToolingError(RuntimeError):
    """Raised at startup when the investigation model can't do tool calling and
    ``ALLOW_TOOLLESS_INVESTIGATION`` is not set."""


class StructuredOutputCapacityError(RuntimeError):
    """Raised at startup when the resolved structured-output model is known to
    reject the investigation engine's response schemas.

    Deliberately has no opt-out flag. ``ALLOW_TOOLLESS_INVESTIGATION`` exists
    because toolless operation is a *coherent degraded mode* — the engine still
    runs, on less evidence. There is no equivalent degraded mode here: every
    investigating turn needs the stage schema, so the engine cannot record state
    at all. Booting anyway would only defer an unavoidable failure to the first
    real case."""


@dataclass(frozen=True)
class InvestigationCapability:
    """Resolved tool-calling capability of the investigation (DA→CHAT) model.

    ``tool_capable`` is the load-bearing field. ``source`` records where the
    provider was resolved from (DA override vs. CHAT fallback) so messages are
    actionable; ``reason`` is a human-readable explanation when not capable.
    """

    tool_capable: bool
    provider: str
    model: str
    source: str
    reason: Optional[str] = None


def resolve_investigation_capability(
    settings: "Settings", registry: Any
) -> InvestigationCapability:
    """Resolve whether the investigation model supports tool calling.

    Pure (never raises): used by both the startup gate and ``/health``.
    ``registry`` is any object exposing ``get_provider(name)`` returning a
    provider with ``supports_tool_calling(model)`` (or ``None`` if uninitialized).
    """
    llm = settings.llm
    da_provider = llm.get_da_provider()
    model = llm.get_da_model()
    provider_name = (
        da_provider.value if hasattr(da_provider, "value") else str(da_provider)
    )
    source = (
        "DA_PROVIDER"
        if llm.da_provider is not None
        else "CHAT_PROVIDER (no DA_PROVIDER override set)"
    )

    provider = None
    try:
        provider = registry.get_provider(provider_name)
    except Exception as exc:  # registry not ready / unknown provider
        return InvestigationCapability(
            tool_capable=False,
            provider=provider_name,
            model=model,
            source=source,
            reason=f"could not resolve provider {provider_name!r}: {exc}",
        )

    if provider is None:
        return InvestigationCapability(
            tool_capable=False,
            provider=provider_name,
            model=model,
            source=source,
            reason=(
                f"provider {provider_name!r} is not initialized "
                "(missing credential, or the provider failed to initialize)"
            ),
        )

    capable = bool(provider.supports_tool_calling(model))
    reason = (
        None
        if capable
        else (
            f"{provider_name}/{model} does not support tool calling "
            f"(resolved from {source})"
        )
    )
    return InvestigationCapability(
        tool_capable=capable,
        provider=provider_name,
        model=model,
        source=source,
        reason=reason,
    )


@dataclass(frozen=True)
class SchemaCapacity:
    """Resolved response-schema capacity of the structured-output model.

    ``can_serve_schemas`` is the load-bearing field. ``inconclusive`` marks the
    cases where capacity could not be determined (registry not ready, provider
    uninitialized, provider predates the capability method) — those must never be
    read as a failure, so the gate fails OPEN on them rather than blocking boot on
    ignorance.
    """

    can_serve_schemas: bool
    provider: str
    model: str
    source: str
    inconclusive: bool = False
    reason: Optional[str] = None


def resolve_schema_capacity(settings: "Settings", registry: Any) -> SchemaCapacity:
    """Resolve whether the structured-output model can serve the engine's schemas.

    Pure (never raises): usable by both the startup gate and ``/health``.
    ``registry`` is any object exposing ``get_provider(name)``; the provider is
    consulted via ``supports_engine_response_schemas(model)``.
    """
    llm = settings.llm
    provider_enum = llm.get_structured_output_provider()
    model = llm.get_structured_output_model()
    provider_name = (
        provider_enum.value if hasattr(provider_enum, "value") else str(provider_enum)
    )
    source = (
        "STRUCTURED_OUTPUT_PROVIDER"
        if llm.structured_output_provider is not None
        else "CHAT_PROVIDER (no STRUCTURED_OUTPUT_PROVIDER override set)"
    )

    def _inconclusive(reason: str) -> SchemaCapacity:
        return SchemaCapacity(
            can_serve_schemas=True,
            provider=provider_name,
            model=model,
            source=source,
            inconclusive=True,
            reason=reason,
        )

    try:
        provider = registry.get_provider(provider_name)
    except Exception as exc:
        return _inconclusive(f"could not resolve provider {provider_name!r}: {exc}")

    if provider is None:
        return _inconclusive(
            f"provider {provider_name!r} is not initialized (missing credential, "
            "or the provider failed to initialize)"
        )

    probe = getattr(provider, "supports_engine_response_schemas", None)
    if probe is None:
        # A provider that predates the capability says nothing either way. The
        # credential and tooling gates already ran; don't invent a verdict.
        return _inconclusive(
            f"provider {provider_name!r} does not report response-schema capacity"
        )

    try:
        capable = bool(probe(model))
    except Exception as exc:
        return _inconclusive(
            f"capacity probe raised for {provider_name}/{model}: {exc}"
        )

    return SchemaCapacity(
        can_serve_schemas=capable,
        provider=provider_name,
        model=model,
        source=source,
        reason=(
            None
            if capable
            else (
                f"{provider_name}/{model} rejects the investigation engine's "
                f"response schemas (resolved from {source})"
            )
        ),
    )


def validate_structured_output_capacity(settings: "Settings", registry: Any) -> None:
    """Fail-fast gate: refuse to boot when the resolved structured-output model is
    known to reject the engine's response schemas.

    Fails OPEN when capacity is inconclusive — an unknown model is assumed capable
    and allowed to boot, because the alternative (blocking on ignorance) would
    refuse every provider that hasn't been measured.

    Raises:
        StructuredOutputCapacityError: the model has a measured, reproducible
            incompatibility with the engine's schemas.
    """
    cap = resolve_schema_capacity(settings, registry)
    if cap.inconclusive:
        logger.debug(
            "Structured-output schema capacity inconclusive for %s/%s: %s — "
            "assuming capable.",
            cap.provider,
            cap.model,
            cap.reason,
        )
        return
    if cap.can_serve_schemas:
        return

    raise StructuredOutputCapacityError(
        f"Structured-output model {cap.provider}/{cap.model} cannot serve the "
        f"investigation engine's response schemas (resolved from {cap.source}). "
        f"The engine sends a per-stage schema on every investigating turn; this "
        f"model accepts the small INQUIRY schema and rejects the larger DIAGNOSIS "
        f"one, so a case would advance a few turns and then fail every remaining "
        f"turn. Use a model with a larger constrained-decoding budget (for "
        f"{cap.provider}, gemini-3.5-flash is the documented default), or set "
        f"STRUCTURED_OUTPUT_PROVIDER to route schema-bound calls to a different "
        f"provider while keeping this one for chat."
    )


def validate_investigation_tooling(settings: "Settings", registry: Any) -> None:
    """Fail-fast gate: refuse to boot on a tool-incapable investigation model
    unless ``ALLOW_TOOLLESS_INVESTIGATION`` is set.

    Raises:
        InvestigationToolingError: tool-incapable investigation model and no
            explicit opt-in.
    """
    cap = resolve_investigation_capability(settings, registry)
    if cap.tool_capable:
        return

    remedy = (
        "Use a tool-capable CHAT_PROVIDER (anthropic, openai, gemini) — or set "
        "DA_PROVIDER to override just the investigation provider — or set "
        "ALLOW_TOOLLESS_INVESTIGATION=true to run in degraded mode "
        "(no search_file/deep_analysis; responses limited to structural-index "
        "summaries)."
    )

    if settings.llm.allow_toolless_investigation:
        # Knowing opt-in — boot, but loudly, and /health will report degraded.
        logger.warning(
            "Investigation model %s/%s does not support tool calling (resolved "
            "from %s); ALLOW_TOOLLESS_INVESTIGATION is set, so booting in "
            "DEGRADED mode: Directed Analysis (search_file, deep_analysis) is "
            "unavailable and investigations are limited to structural-index "
            "summaries.",
            cap.provider,
            cap.model,
            cap.source,
        )
        return

    raise InvestigationToolingError(
        f"Investigation model {cap.provider}/{cap.model} does not support tool "
        f"calling (resolved from {cap.source}). The investigation engine needs "
        f"tool calling to gather evidence (search_file, deep_analysis); without "
        f"it, it would draw conclusions without reaching the evidence. " + remedy
    )
