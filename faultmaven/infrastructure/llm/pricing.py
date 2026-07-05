"""LLM cost estimation.

Maps ``(provider, model)`` to per-token USD rates so token consumption can be
expressed in dollars — the unit an operator actually notices when a bill spikes.

**These rates are operator-maintainable ESTIMATES, not billing truth.** Provider
prices change and this table will drift. Two guarantees keep the drift honest:

1. Override any rate at runtime via the ``LLM_PRICING_OVERRIDES`` env var (JSON)
   without touching code — e.g. after a provider price change.
2. An unknown ``(provider, model)`` is reported as *unpriced* (cost ``0.0`` and
   ``priced=False``) rather than guessed. Callers surface unpriced calls on a
   separate counter, so dashboards UNDER-count visibly instead of lying.

Rates are quoted per 1M tokens and split into four disjoint buckets that match
:class:`~faultmaven.infrastructure.llm.providers.base.LLMResponse`:
``input`` (uncached prompt), ``output`` (completion), ``cache_read`` (prompt
tokens served from the provider's prompt cache, billed at a discount) and
``cache_write`` (tokens written to the provider's prompt cache, sometimes billed
at a premium). A model that doesn't do prompt caching simply has ``0`` for the
cache buckets.

This module has no third-party dependencies on purpose (no prometheus, no
settings import) so it stays trivially unit-testable and safe to import from the
hot path.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Env var carrying a JSON object of rate overrides, shaped like DEFAULT_RATES:
#   {"anthropic": {"claude-sonnet-4-6": {"input": 3.0, "output": 15.0,
#                                        "cache_read": 0.3, "cache_write": 3.75}}}
# Any subset is allowed; provided providers/models are merged over the defaults.
_OVERRIDES_ENV = "LLM_PRICING_OVERRIDES"


@dataclass(frozen=True)
class TokenRates:
    """USD per 1,000,000 tokens, per bucket. All buckets disjoint."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


# Best-effort published rates as of mid-2026 for the models FaultMaven ships
# with (see repo CLAUDE.md "Supported LLM Providers"). Keys are lowercased and
# matched by substring, so an entry like "claude-sonnet-4-6" also matches
# "anthropic/claude-sonnet-4-6" (OpenRouter) or a dated snapshot suffix.
# Anthropic prompt-cache: write ~1.25x input (5-min TTL), read ~0.1x input.
# OpenAI-family cached input is ~0.5x input; write is not separately billed.
DEFAULT_RATES: dict[str, dict[str, TokenRates]] = {
    "anthropic": {
        # Sonnet tier: $3 input / $15 output per 1M, cache write ~1.25x, read ~0.1x.
        "claude-sonnet-4-5": TokenRates(3.0, 15.0, 0.30, 3.75),
        "claude-sonnet-4-6": TokenRates(3.0, 15.0, 0.30, 3.75),
        "claude-opus-4": TokenRates(15.0, 75.0, 1.50, 18.75),
        "claude-haiku": TokenRates(0.80, 4.0, 0.08, 1.0),
    },
    "openai": {
        # gpt-4.1-mini is FaultMaven's default OpenAI model (non-reasoning).
        # Public rates: $0.40 input / $1.60 output per 1M, cached input $0.10/1M.
        "gpt-4.1-mini": TokenRates(0.40, 1.60, 0.10, 0.0),
        # gpt-5.4-mini stays a pinnable reasoning option.
        "gpt-5.4-mini": TokenRates(0.15, 0.60, 0.075, 0.0),
        "gpt-4o-mini": TokenRates(0.15, 0.60, 0.075, 0.0),
        "gpt-4o": TokenRates(2.50, 10.0, 1.25, 0.0),
    },
    "gemini": {
        "gemini-3.5-flash": TokenRates(0.15, 0.60, 0.0375, 0.0),
        "gemini-1.5-flash": TokenRates(0.075, 0.30, 0.01875, 0.0),
        "gemini-1.5-pro": TokenRates(1.25, 5.0, 0.3125, 0.0),
    },
    "fireworks": {
        "deepseek-v3": TokenRates(0.90, 0.90, 0.0, 0.0),
        "deepseek": TokenRates(0.90, 0.90, 0.0, 0.0),
    },
    "groq": {
        "llama-3.3-70b": TokenRates(0.59, 0.79, 0.0, 0.0),
        "llama-3.1-8b": TokenRates(0.05, 0.08, 0.0, 0.0),
    },
    "cohere": {
        "command-r-plus": TokenRates(2.50, 10.0, 0.0, 0.0),
        "command-r": TokenRates(0.15, 0.60, 0.0, 0.0),
    },
    "openrouter": {
        # OpenRouter passes provider rates through; keep the common routed models.
        "claude-sonnet-4-5": TokenRates(3.0, 15.0, 0.30, 3.75),
        "claude-sonnet-4-6": TokenRates(3.0, 15.0, 0.30, 3.75),
    },
}

# Providers with no per-token cost (self-hosted). These are KNOWN to be free, so
# they price to $0 with priced=True — distinct from an unknown model, which is
# unpriced=True so cost under-reporting stays visible.
_ZERO_COST_PROVIDERS = frozenset({"local", "huggingface"})
_ZERO_RATES = TokenRates()


def _load_overrides() -> dict[str, dict[str, TokenRates]]:
    raw = os.getenv(_OVERRIDES_ENV)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("Ignoring invalid %s (not JSON): %s", _OVERRIDES_ENV, exc)
        return {}
    result: dict[str, dict[str, TokenRates]] = {}
    for provider, models in (parsed or {}).items():
        if not isinstance(models, dict):
            continue
        table: dict[str, TokenRates] = {}
        for model, rates in models.items():
            if not isinstance(rates, dict):
                continue
            table[str(model).lower()] = TokenRates(
                input=float(rates.get("input", 0.0)),
                output=float(rates.get("output", 0.0)),
                cache_read=float(rates.get("cache_read", 0.0)),
                cache_write=float(rates.get("cache_write", 0.0)),
            )
        result[str(provider).lower()] = table
    return result


def _build_rates() -> dict[str, dict[str, TokenRates]]:
    rates = {p: dict(models) for p, models in DEFAULT_RATES.items()}
    for provider, models in _load_overrides().items():
        rates.setdefault(provider, {})
        rates[provider].update(models)
    return rates


# Resolved once at import; call reload_rates() in tests after mutating the env.
_RATES: dict[str, dict[str, TokenRates]] = _build_rates()


def reload_rates() -> None:
    """Re-read DEFAULT_RATES + LLM_PRICING_OVERRIDES. Primarily for tests."""
    global _RATES
    _RATES = _build_rates()


def _normalize_provider(provider: str | None) -> str:
    # Registry tags low-confidence fallbacks as e.g. "anthropic (low-confidence)".
    name = (provider or "").lower().strip()
    return name.split(" ", 1)[0]


def lookup_rates(provider: str | None, model: str | None) -> TokenRates | None:
    """Return rates for (provider, model), or None if unpriced."""
    name = _normalize_provider(provider)
    if name in _ZERO_COST_PROVIDERS:
        return _ZERO_RATES
    table = _RATES.get(name)
    if not table:
        return None
    model_l = (model or "").lower()
    if model_l in table:
        return table[model_l]
    # Substring match handles provider-prefixed or dated model ids.
    for key, rates in table.items():
        if key and key in model_l:
            return rates
    return None


def estimate_cost_usd(
    provider: str | None,
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple[float, bool]:
    """Estimate USD cost for one call from its disjoint token buckets.

    Returns ``(cost_usd, priced)``. When the model is not in the table,
    returns ``(0.0, False)`` so callers can count the call as *unpriced* rather
    than silently treating it as free.
    """
    rates = lookup_rates(provider, model)
    if rates is None:
        return 0.0, False
    cost = (
        input_tokens * rates.input
        + output_tokens * rates.output
        + cache_read_tokens * rates.cache_read
        + cache_write_tokens * rates.cache_write
    ) / 1_000_000.0
    return cost, True
