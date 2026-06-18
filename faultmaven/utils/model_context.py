"""Model context-window registry (GAP-1).

Resolves a provider/model pair to its *real* context window and a derived
prompt-token budget, replacing the hand-tuned per-provider prompt-number table
that ``get_token_budget_for_provider`` used to embed inline.

Why a registry instead of a lookup table of prompt budgets:

- The old table returned an opaque "prompt budget" (e.g. Gemini → 15K) that was
  a fraction of the *real* window (~1M) chosen by hand. That conflated two
  separate facts — the model's window and how much of it we choose to spend on
  the prompt — and drifted silently as models were renamed/released.
- This registry stores the **true context window** keyed by exact model id (with
  family-prefix fallbacks), plus a recommended **response reserve**. The prompt
  budget is then *derived*: ``prompt_budget = window − response_reserve``. The
  policy (reserve) is explicit and configurable, separate from the fact (window).

Resolution order for a (provider, model):

1. Operator override map (``MODEL_CONTEXT_WINDOWS`` env / settings), exact id.
2. Built-in registry, exact id.
3. Built-in registry, longest matching family prefix.
4. Conservative default → ``logger.warning`` and ``used_default=True``.

The conservative default is intentionally a *small* window so unknown models
under-fill rather than risk overflow (GAP-1 §5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# Conservative default window for unrecognized models. Small on purpose: an
# unknown model should under-fill (safe) rather than over-fill (overflow risk).
# 16K window − 6K reserve → 10K prompt budget, matching the old table's
# mid-range Claude value so behavior for unknown models is not a regression.
DEFAULT_CONTEXT_WINDOW = 16_000
# Default response reserve when a registry entry doesn't specify one.
DEFAULT_RESPONSE_RESERVE = 6_000
# Floor so a tiny/misconfigured window can never yield a non-positive budget.
MIN_PROMPT_BUDGET = 2_000


@dataclass(frozen=True)
class ModelWindow:
    """A model's context window and recommended response reserve, in tokens."""

    context_window: int
    response_reserve: int = DEFAULT_RESPONSE_RESERVE


@dataclass(frozen=True)
class ResolvedBudget:
    """Result of resolving a (provider, model) to a token budget.

    Two distinct budgets (see module docstring + ``resolve_prompt_target``):

    - ``prompt_budget`` — the **hard ceiling**: the largest prompt the model
      can physically accept while leaving the response reserve
      (``context_window − response_reserve``, floored). This is the overflow
      threshold the GAP-3 backstop checks against.
    - ``prompt_target`` — the **soft target**: how much of the window we
      actually choose to fill on a routine turn, for cost/quality control
      (``clamp(window × fraction, floor, cap)``, never above ``prompt_budget``).
      This is what the section/evidence fills are sized against.

    The remaining fields are for observability (per-turn logging, admin/debug).
    """

    provider: str
    model: Optional[str]
    context_window: int
    response_reserve: int
    prompt_budget: int
    prompt_target: int
    matched_key: Optional[str]
    used_default: bool


# =============================================================================
# Built-in registry. Keys are matched case-insensitively, exact-first then by
# longest prefix. Windows are the published context windows (tokens); reserves
# are conservative response allowances. Maintained by hand — operators can
# override or extend via MODEL_CONTEXT_WINDOWS without a code change.
# =============================================================================
_REGISTRY: Dict[str, ModelWindow] = {
    # --- Anthropic Claude (200K standard; 1M with the [1m] beta context) ---
    "claude-opus-4": ModelWindow(200_000, 8_000),
    "claude-sonnet-4": ModelWindow(200_000, 8_000),
    "claude-haiku-4": ModelWindow(200_000, 8_000),
    "claude-3-5-sonnet": ModelWindow(200_000, 8_000),
    "claude-3-5-haiku": ModelWindow(200_000, 8_000),
    "claude-3-opus": ModelWindow(200_000, 8_000),
    "claude-3-sonnet": ModelWindow(200_000, 8_000),
    "claude-3-haiku": ModelWindow(200_000, 8_000),
    "claude": ModelWindow(200_000, 8_000),  # family fallback
    # --- OpenAI ---
    "gpt-5": ModelWindow(400_000, 16_000),
    "gpt-4.1": ModelWindow(1_000_000, 16_000),
    "gpt-4o": ModelWindow(128_000, 8_000),
    "gpt-4-turbo": ModelWindow(128_000, 8_000),
    "gpt-4": ModelWindow(8_192, 2_000),  # legacy 8K base
    "gpt-3.5-turbo": ModelWindow(16_385, 2_000),
    "o1": ModelWindow(200_000, 16_000),
    "o3": ModelWindow(200_000, 16_000),
    # --- Google Gemini ---
    "gemini-3": ModelWindow(1_000_000, 16_000),
    "gemini-2": ModelWindow(1_000_000, 16_000),
    "gemini-1.5-pro": ModelWindow(2_000_000, 16_000),
    "gemini-1.5-flash": ModelWindow(1_000_000, 8_000),
    "gemini": ModelWindow(1_000_000, 16_000),  # family fallback
    # --- Meta Llama ---
    "llama-3.3": ModelWindow(128_000, 8_000),
    "llama-3.1": ModelWindow(128_000, 8_000),
    "llama-3": ModelWindow(8_192, 2_000),
    "llama": ModelWindow(8_192, 2_000),  # family fallback
    # --- Fireworks (DeepSeek and friends; OpenAI-compatible serving) ---
    "accounts/fireworks/models/deepseek-v3": ModelWindow(128_000, 8_000),
    "deepseek-v3": ModelWindow(128_000, 8_000),
    "deepseek": ModelWindow(64_000, 6_000),
    # --- Cohere ---
    "command-r-plus": ModelWindow(128_000, 6_000),
    "command-r": ModelWindow(128_000, 6_000),
    "command": ModelWindow(4_096, 1_500),
}

# Provider-name → registry-prefix hints, used when the model id is missing or
# unrecognized but the provider is known (so we still resolve a sane window
# instead of dropping straight to the conservative default).
_PROVIDER_FALLBACK_KEY: Dict[str, str] = {
    "anthropic": "claude",
    "openai": "gpt-4o",
    "openrouter": "gpt-4o",
    "google": "gemini",
    "gemini": "gemini",
    "meta": "llama",
    "fireworks": "deepseek",
    "groq": "llama",
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


def resolve_model_budget(
    provider_name: Optional[str], model_name: Optional[str] = None
) -> ResolvedBudget:
    """Resolve a (provider, model) to its window + derived prompt budget.

    See module docstring for the resolution order. Never raises; on an
    unrecognized model it logs a WARNING and returns a conservative default
    with ``used_default=True``.
    """
    provider_lower = _normalize(provider_name)
    model_lower = _normalize(model_name)

    # 1. Operator overrides (exact id), then 2/3. built-in (exact, then prefix).
    overrides = _get_overrides()
    key, window = _lookup(overrides, model_lower)
    if window is None:
        key, window = _lookup(_REGISTRY, model_lower)

    used_default = False
    if window is None:
        # 3b. Provider-level fallback: known provider, unknown/blank model id.
        fallback_key = _PROVIDER_FALLBACK_KEY.get(provider_lower)
        if fallback_key and fallback_key in _REGISTRY:
            key, window = fallback_key, _REGISTRY[fallback_key]
            logger.warning(
                "Model %r unrecognized for provider %r; using provider family "
                "fallback %r (window=%d).",
                model_name,
                provider_name,
                fallback_key,
                window.context_window,
            )
        else:
            # 4. Conservative default.
            key = None
            window = ModelWindow(DEFAULT_CONTEXT_WINDOW, DEFAULT_RESPONSE_RESERVE)
            used_default = True
            logger.warning(
                "Unknown model %r for provider %r; using conservative default "
                "window=%d, reserve=%d (prompt_budget=%d). Add it to "
                "MODEL_CONTEXT_WINDOWS to tune.",
                model_name,
                provider_name,
                window.context_window,
                window.response_reserve,
                max(MIN_PROMPT_BUDGET, window.context_window - window.response_reserve),
            )

    prompt_budget = max(
        MIN_PROMPT_BUDGET, window.context_window - window.response_reserve
    )
    prompt_target = _soft_target(window.context_window, prompt_budget)
    return ResolvedBudget(
        provider=provider_lower or "unknown",
        model=model_name,
        context_window=window.context_window,
        response_reserve=window.response_reserve,
        prompt_budget=prompt_budget,
        prompt_target=prompt_target,
        matched_key=key,
        used_default=used_default,
    )


# Defaults mirror ModelContextSettings so the registry is usable without a
# settings load (tests / internal callers).
_DEFAULT_TARGET_CAP = 32_000
_DEFAULT_TARGET_FRACTION = 0.5
_DEFAULT_TARGET_FLOOR = 6_000


def _soft_target(context_window: int, hard_prompt_budget: int) -> int:
    """Soft routine-fill target: ``clamp(window×fraction, floor, cap)`` capped
    at the hard budget. See ``ResolvedBudget.prompt_target``.
    """
    cap, fraction, floor = (
        _DEFAULT_TARGET_CAP,
        _DEFAULT_TARGET_FRACTION,
        _DEFAULT_TARGET_FLOOR,
    )
    try:
        from faultmaven.config.settings import get_settings

        mc = get_settings().model_context
        cap, fraction, floor = (
            mc.prompt_target_cap,
            mc.prompt_target_fraction,
            mc.prompt_target_floor,
        )
    except Exception:
        pass
    target = min(cap, max(floor, int(context_window * fraction)))
    return min(hard_prompt_budget, target)
