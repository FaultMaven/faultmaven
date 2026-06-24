"""Startup fail-fast for investigation tool-calling capability.

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

Scope is the **tool-calling** axis of the DA→CHAT investigation provider only.
``STRUCTURED_OUTPUT_PROVIDER`` (which routes schema-bound calls to a possibly
different provider) is a *separate* axis — schema enforcement — and is
deliberately NOT gated here: structured output uses ``json_schema`` /
``response_format``, which does not require tool calling, so a tool-incapable
structured-output provider is not a problem this gate needs to catch.

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
        "Set DA_PROVIDER to a tool-capable provider (anthropic, openai, gemini), "
        "or set ALLOW_TOOLLESS_INVESTIGATION=true to run in degraded mode "
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
