"""Model context-window registry (GAP-1).

The prompt budget FaultMaven sends is **operator-owned**: it is a flat target
(``PROMPT_TARGET_TOKENS``, default 32K in ``.env.example``) driven by what the
investigation task needs — NOT a fraction of the model window. Prompt tokens are
a scarce resource budget-allocated programmatically; a flat target protects
fleet cost on big-window models and forces the agent onto RAG tools instead of
lazy context-dumping.

This module is therefore deliberately a *thin, optional safety net*, not an
authority anyone must maintain:

- The **budget** is ``PROMPT_TARGET_TOKENS``. Full stop.
- The model window enters only as a best-effort **downward clamp** for the small
  number of models we happen to know: ``prompt_target = min(target, window −
  reserve)``. On the curated big-window flagships this clamp never binds; on a
  small/local model it trims the target to fit.
- **Unknown / uncurated model → we silently trust the configured target.** No
  warning, no conservative clamp. This is the *expected* case for local/custom
  models, not a failure — so the registry being incomplete is harmless and
  nobody has to keep it accurate.

Resolution order for a (provider, model):

1. Operator override map (``MODEL_CONTEXT_WINDOWS`` env / settings), exact id.
2. Built-in registry, exact id.
3. Built-in registry, longest matching family prefix.
4. Provider-family fallback (known provider, new/blank model id).
5. Unknown → window unknown, trust the target (``window_known=False``).

Operators owning a small/local model set ``PROMPT_TARGET_TOKENS`` to fit it
(documented in ``.env.example``); they never touch a window table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# Default response reserve when a registry entry doesn't specify one.
DEFAULT_RESPONSE_RESERVE = 6_000
# Floor so a tiny/misconfigured window can never yield a non-positive budget.
MIN_PROMPT_BUDGET = 2_000
# Mirrors ModelContextSettings.prompt_target_tokens so the module is usable
# without a settings load (tests / internal callers).
_DEFAULT_PROMPT_TARGET = 32_000


@dataclass(frozen=True)
class ModelWindow:
    """A model's context window and recommended response reserve, in tokens."""

    context_window: int
    response_reserve: int = DEFAULT_RESPONSE_RESERVE


@dataclass(frozen=True)
class ResolvedBudget:
    """Result of resolving a (provider, model) to a token budget.

    - ``prompt_target`` — the **budget**: what FaultMaven actually fills, the
      operator-owned flat ``PROMPT_TARGET_TOKENS`` clamped down to the model's
      hard ceiling *when that ceiling is known*. This is what the
      section/evidence fills are sized against and is always populated.
    - ``prompt_budget`` / ``context_window`` / ``response_reserve`` — the
      **hard ceiling** and its inputs, populated only when the model's window is
      known (``window_known=True``); ``None`` otherwise. The GAP-3 overflow
      backstop uses ``prompt_budget`` when present and skips the window check
      when it is ``None`` (we trust the operator's target).
    - ``window_known`` — ``False`` for unknown/uncurated models. Not an error:
      the budget is still the configured target; we simply could not verify the
      window. Surfaced in ``/debug`` so an operator can see it.
    """

    provider: str
    model: Optional[str]
    prompt_target: int
    context_window: Optional[int]
    response_reserve: Optional[int]
    prompt_budget: Optional[int]
    matched_key: Optional[str]
    window_known: bool


# =============================================================================
# Built-in registry — the OPTIONAL overflow safety net.
#
# Intentionally curated, NOT exhaustive. It contains ONLY large-context models
# we are confident comfortably exceed the default PROMPT_TARGET_TOKENS (32K), so
# a registry hit is always "this model is big — the flat target fits." Small /
# legacy / ambiguous models are deliberately ABSENT: they resolve to
# window_known=False and we trust the operator's configured target (who lowers
# PROMPT_TARGET_TOKENS to fit). That keeps the registry free of entries whose
# only effect would be to clamp operators down based on our guess.
#
# Keys match case-insensitively, exact-first then longest prefix. Operators can
# extend/override via MODEL_CONTEXT_WINDOWS without a code change.
# =============================================================================
_REGISTRY: Dict[str, ModelWindow] = {
    # --- Anthropic Claude (200K standard; 1M with the [1m] beta context) ---
    "claude-opus-4": ModelWindow(200_000, 8_000),
    "claude-sonnet-4": ModelWindow(200_000, 8_000),
    "claude-haiku-4": ModelWindow(200_000, 8_000),
    "claude-3-5-sonnet": ModelWindow(200_000, 8_000),
    "claude-3-5-haiku": ModelWindow(200_000, 8_000),
    "claude-3-opus": ModelWindow(200_000, 8_000),
    "claude": ModelWindow(200_000, 8_000),  # family fallback (all ≥200K)
    # --- OpenAI (only the ≥128K models; legacy gpt-4 8K / gpt-3.5 16K omitted) ---
    # gpt-5.6 publishes a 1,050,000 TOTAL window but caps input at 922,000, and
    # this number is consumed as a prompt ceiling (budget = window - reserve),
    # so the max-INPUT bound is the correct one to record: using the total
    # would authorise a prompt the API rejects. Listed before the "gpt-5"
    # family entry, which would otherwise claim it by prefix and under-report
    # its window by 2.3x.
    "gpt-5.6": ModelWindow(922_000, 16_000),
    "gpt-5": ModelWindow(400_000, 16_000),
    "gpt-4.1": ModelWindow(1_000_000, 16_000),
    "gpt-4o": ModelWindow(128_000, 8_000),
    "gpt-4-turbo": ModelWindow(128_000, 8_000),
    "o1": ModelWindow(200_000, 16_000),
    "o3": ModelWindow(200_000, 16_000),
    # --- Google Gemini (all ≥1M) ---
    "gemini-3": ModelWindow(1_000_000, 16_000),
    "gemini-2": ModelWindow(1_000_000, 16_000),
    "gemini-1.5-pro": ModelWindow(2_000_000, 16_000),
    "gemini-1.5-flash": ModelWindow(1_000_000, 8_000),
    "gemini": ModelWindow(1_000_000, 16_000),  # family fallback
    # --- Meta Llama (only the 128K models; generic/8K llama omitted so local
    #     Ollama llamas fall to trust-the-operator instead of a 8K clamp) ---
    "llama-3.3": ModelWindow(128_000, 8_000),
    "llama-3.1": ModelWindow(128_000, 8_000),
    # --- Fireworks (DeepSeek; OpenAI-compatible serving) ---
    "accounts/fireworks/models/deepseek-v3": ModelWindow(128_000, 8_000),
    "deepseek-v3": ModelWindow(128_000, 8_000),
    # --- Cohere (only the 128K command-r line; legacy 4K command omitted) ---
    "command-r-plus": ModelWindow(128_000, 6_000),
    "command-r": ModelWindow(128_000, 6_000),
}

# Provider → registry-prefix hint, used ONLY when the model id is blank/new but
# the provider is known, to map to that provider's large flagship. Providers
# whose served model/window genuinely varies (local, groq, huggingface) are
# deliberately ABSENT → unknown model → trust the operator's target.
_PROVIDER_FALLBACK_KEY: Dict[str, str] = {
    "anthropic": "claude",
    # Each entry names the provider's own shipped default family, so a blank or
    # brand-new model id inherits a representative window rather than a legacy
    # one. These drifted when the defaults moved: openai pointed at gpt-4o
    # (128K) while shipping a 922K model, and openrouter pointed at gpt-4o
    # while defaulting to anthropic/claude-sonnet-4-6.
    "openai": "gpt-5.6",
    "openrouter": "claude",
    "google": "gemini",
    "gemini": "gemini",
    "fireworks": "deepseek-v3",
    "cohere": "command-r",
}


def _normalize(text: Optional[str]) -> str:
    return text.lower().strip() if text else ""


def _lookup(
    registry: Dict[str, ModelWindow], model_lower: str
) -> Tuple[Optional[str], Optional[ModelWindow]]:
    """Exact match, then longest matching prefix. Returns (key, window)."""
    if not model_lower:
        return None, None
    if model_lower in registry:
        return model_lower, registry[model_lower]
    # Longest prefix match — prefer the most specific family entry.
    best_key: Optional[str] = None
    for key in registry:
        if model_lower.startswith(key) and (
            best_key is None or len(key) > len(best_key)
        ):
            best_key = key
    if best_key is not None:
        return best_key, registry[best_key]
    return None, None


def _get_overrides() -> Dict[str, ModelWindow]:
    """Read operator overrides from settings (best-effort, cached).

    Override format (env ``MODEL_CONTEXT_WINDOWS`` as JSON, see settings):
    ``{"my-model": {"context_window": 200000, "response_reserve": 8000}}``.
    """
    try:
        from faultmaven.config.settings import get_settings

        raw = get_settings().model_context.window_overrides
    except Exception:
        return {}
    out: Dict[str, ModelWindow] = {}
    for key, val in (raw or {}).items():
        try:
            out[key.lower().strip()] = ModelWindow(
                context_window=int(val["context_window"]),
                response_reserve=int(
                    val.get("response_reserve", DEFAULT_RESPONSE_RESERVE)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Ignoring malformed MODEL_CONTEXT_WINDOWS override for %r: %s",
                key,
                exc,
            )
    return out


def _configured_target() -> int:
    """The operator-owned flat prompt target (``PROMPT_TARGET_TOKENS``).

    Falls back to ``_DEFAULT_PROMPT_TARGET`` when settings are unavailable
    (tests / internal callers).
    """
    try:
        from faultmaven.config.settings import get_settings

        return get_settings().model_context.prompt_target_tokens
    except Exception:
        return _DEFAULT_PROMPT_TARGET


def resolve_model_budget(
    provider_name: Optional[str], model_name: Optional[str] = None
) -> ResolvedBudget:
    """Resolve a (provider, model) to its prompt budget.

    See module docstring for the resolution order. Never raises. The budget is
    always the operator-owned ``PROMPT_TARGET_TOKENS``; the model window only
    clamps it down when we happen to know it. An unknown/uncurated model is NOT
    an error — we trust the configured target and set ``window_known=False``.
    """
    provider_lower = _normalize(provider_name)
    model_lower = _normalize(model_name)
    target = _configured_target()

    # 1. Operator overrides (exact id), then 2/3. built-in (exact, then prefix).
    overrides = _get_overrides()
    key, window = _lookup(overrides, model_lower)
    if window is None:
        key, window = _lookup(_REGISTRY, model_lower)

    # 4. Provider-family fallback: known provider, new/blank model id.
    if window is None:
        fallback_key = _PROVIDER_FALLBACK_KEY.get(provider_lower)
        if fallback_key and fallback_key in _REGISTRY:
            key, window = fallback_key, _REGISTRY[fallback_key]
            logger.debug(
                "Model %r not in registry for provider %r; using family "
                "fallback %r (window=%d) for the overflow safety net.",
                model_name,
                provider_name,
                fallback_key,
                window.context_window,
            )

    # 5. Unknown / uncurated → trust the configured target (no clamp, no warn).
    if window is None:
        logger.debug(
            "Window unknown for provider %r model %r; trusting configured "
            "PROMPT_TARGET_TOKENS=%d. Set it to fit your model if it is small.",
            provider_name,
            model_name,
            target,
        )
        return ResolvedBudget(
            provider=provider_lower or "unknown",
            model=model_name,
            prompt_target=max(MIN_PROMPT_BUDGET, target),
            context_window=None,
            response_reserve=None,
            prompt_budget=None,
            matched_key=None,
            window_known=False,
        )

    # Known model: clamp the flat target down to the hard ceiling (free safety).
    prompt_budget = max(
        MIN_PROMPT_BUDGET, window.context_window - window.response_reserve
    )
    prompt_target = max(MIN_PROMPT_BUDGET, min(target, prompt_budget))
    return ResolvedBudget(
        provider=provider_lower or "unknown",
        model=model_name,
        prompt_target=prompt_target,
        context_window=window.context_window,
        response_reserve=window.response_reserve,
        prompt_budget=prompt_budget,
        matched_key=key,
        window_known=True,
    )
