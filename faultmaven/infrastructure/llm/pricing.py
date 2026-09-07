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
from datetime import date

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
#
# A key that names no version is a TRAP, because the failure it produces is
# the invisible one: it keeps matching the next generation and bills it at the
# old rate, staying priced=True and so never reaching the unpriced counter.
# Prefer a versioned key and let an unknown generation read as unpriced.
# Known version-blind keys REMAINING after #1359, deliberately left because
# nothing in PROVIDER_SCHEMA offers a model that would be mis-keyed by them
# today: fireworks "deepseek" (a genuine catch-all beside the specific
# deepseek-v3/v4-flash rows) and cohere "command-r-plus"/"command-r" (which
# would capture a future dated or re-priced Command R). Price them
# specifically the moment either provider ships a generation at a new rate.
DEFAULT_RATES: dict[str, dict[str, TokenRates]] = {
    "anthropic": {
        # Sonnet tier: $3 input / $15 output per 1M, cache write ~1.25x, read ~0.1x.
        "claude-sonnet-4-5": TokenRates(3.0, 15.0, 0.30, 3.75),
        "claude-sonnet-4-6": TokenRates(3.0, 15.0, 0.30, 3.75),
        # Opus tier. A version-blind key here fails in TWO directions, and only
        # one of them is visible: it can stop matching (unpriced, cost 0.0,
        # counted on the unpriced counter — the module's designed failure), or
        # it can KEEP matching at a stale rate, which stays priced=True and so
        # reaches no counter at all. The second is strictly worse, and it is
        # what "claude-opus-4" alone was doing.
        #
        # Opus 4 and 4.1 were $15 in / $75 out. Opus repriced to $5 / $25 at
        # 4.5 and has held there through 4.6, 4.7, 4.8 and Opus 5. So the
        # generic key stays — it is still correct for the ids it was written
        # for — and EVERY repriced generation gets its own longer key, which
        # wins by longest-match. Keying only some of them is what the first
        # cut of #1359 did: 4-6 was fixed while 4-5/4-7/4-8 kept billing at
        # $90 per 1M in+out against a real $30, with the new
        # selected_model_priced observable reporting True over it.
        "claude-opus-4": TokenRates(15.0, 75.0, 1.50, 18.75),
        "claude-opus-4-5": TokenRates(5.0, 25.0, 0.50, 6.25),
        "claude-opus-4-6": TokenRates(5.0, 25.0, 0.50, 6.25),
        "claude-opus-4-7": TokenRates(5.0, 25.0, 0.50, 6.25),
        "claude-opus-4-8": TokenRates(5.0, 25.0, 0.50, 6.25),
        "claude-opus-5": TokenRates(5.0, 25.0, 0.50, 6.25),
        # Haiku 4.5 is $1 in / $5 out. The key is VERSIONED for the same
        # reason the Opus keys are: the bare "claude-haiku" it replaces was
        # fully version-blind, so it silently under-reported the shipped
        # picker model (claude-haiku-4-5-20251001) at $0.80/$4.00 and would
        # have handed the same stale rate to every future Haiku generation.
        # Dropping the bare key means a future claude-haiku-5 reads as
        # UNPRICED until someone adds it — visibly wrong instead of quietly
        # wrong, which is the trade this module exists to make.
        "claude-haiku-4-5": TokenRates(1.0, 5.0, 0.10, 1.25),
    },
    "openai": {
        # gpt-5.6-luna is FaultMaven's default OpenAI model. SHORT-CONTEXT
        # rates ($0.20 in / $1.20 out / $0.02 cache read / $0.25 cache write
        # per 1M). OpenAI bills a LONG-CONTEXT tier (2x in / 1.5x out) on
        # requests whose input exceeds 272K tokens; the flat rate here is the
        # short tier because PROMPT_TARGET_TOKENS defaults to 32K — an order of
        # magnitude below the threshold even after the tool loop accumulates.
        # A deployment that raises the budget near 272K should override via
        # LLM_PRICING_OVERRIDES rather than trust this row.
        "gpt-5.6-luna": TokenRates(0.20, 1.20, 0.02, 0.25),
        # gpt-5.4-mini stays a supported non-default (reasoning; #644).
        # Corrected 2026-08-26 against developers.openai.com/api/docs/pricing:
        # the entry read 0.15/0.60/0.075, which is gpt-4o-mini's row — a
        # copy-paste that under-reported this model 5x on input and 7.5x on
        # output. Real rates: $0.75 in / $4.50 out, cached input $0.075/1M.
        "gpt-5.4-mini": TokenRates(0.75, 4.50, 0.075, 0.0),
        # gpt-4.1-mini stays a supported non-default (non-reasoning) option.
        # Public rates: $0.40 input / $1.60 output per 1M, cached input $0.10/1M.
        "gpt-4.1-mini": TokenRates(0.40, 1.60, 0.10, 0.0),
        "gpt-4o-mini": TokenRates(0.15, 0.60, 0.075, 0.0),
        "gpt-4o": TokenRates(2.50, 10.0, 1.25, 0.0),
    },
    "gemini": {
        # Gemini output rates INCLUDE thinking tokens (the provider folds
        # thoughtsTokenCount into output_tokens), so uncapped thinking bills at
        # the full output rate — one more reason thinkingLevel stays at "low".
        #
        # gemini-3.7-flash: Google's INTRODUCTORY rate ($0.75 in / $3.75 out,
        # cache read $0.075), valid through 2026-12-31. The switch to standard
        # pricing on 2027-01-01 is MECHANIZED in _SCHEDULED_RATE_CHANGES below
        # — resolved against today's date at load, so a process started in
        # 2027 prices correctly without a code change. (A process running
        # across the boundary keeps the load-time rate until restart or
        # reload_rates().)
        "gemini-3.7-flash": TokenRates(0.75, 3.75, 0.075, 0.0),
        "gemini-3.5-flash-lite": TokenRates(0.30, 2.50, 0.03, 0.0),
        "gemini-3.5-flash": TokenRates(1.50, 9.0, 0.15, 0.0),
        "gemini-3.1-flash-lite": TokenRates(0.25, 1.50, 0.025, 0.0),
        "gemini-1.5-flash": TokenRates(0.075, 0.30, 0.01875, 0.0),
        "gemini-1.5-pro": TokenRates(1.25, 5.0, 0.3125, 0.0),
    },
    "fireworks": {
        # Specific deepseek-v4-flash key beside the generic "deepseek"
        # fallback — the substring scan is LONGEST-match-wins (see
        # lookup_rates), so the specific key beats the generic one regardless
        # of dict order, including when an operator override appends keys
        # after all built-ins. Standard serverless tier
        # ($0.14 in / $0.28 out / $0.03 cached in per 1M; Priority is higher).
        "deepseek-v4-flash": TokenRates(0.14, 0.28, 0.03, 0.0),
        "deepseek-v3": TokenRates(0.90, 0.90, 0.0, 0.0),
        "deepseek": TokenRates(0.90, 0.90, 0.0, 0.0),
    },
    "groq": {
        "llama-3.3-70b": TokenRates(0.59, 0.79, 0.0, 0.0),
        "llama-3.1-8b": TokenRates(0.05, 0.08, 0.0, 0.0),
        # Groq's only STRICT structured-output models, and therefore the only
        # ones here fit to be CHAT_PROVIDER. Keys are the bare model names so
        # they also match the "openai/"-prefixed ids the picker and GROQ_MODEL
        # use — the same substring convention as the claude-* keys. Cached
        # input is half the input rate (OpenAI-family caching); Groq does not
        # bill a separate cache write.
        "gpt-oss-120b": TokenRates(0.15, 0.60, 0.075, 0.0),
        "gpt-oss-20b": TokenRates(0.075, 0.30, 0.0375, 0.0),
    },
    "cohere": {
        "command-r-plus": TokenRates(2.50, 10.0, 0.0, 0.0),
        "command-r": TokenRates(0.15, 0.60, 0.0, 0.0),
    },
    "openrouter": {
        # OpenRouter passes provider rates through; keep the common routed
        # models. The Opus rows mirror the anthropic table above: routing
        # "anthropic/claude-opus-5" through OpenRouter is the same model at
        # the same published rate, and pricing it only on the direct path
        # would have left goal (b) of #1359 half-met — an Opus 5 deployment
        # reporting $0 purely because it reaches Anthropic via the gateway.
        "claude-sonnet-4-5": TokenRates(3.0, 15.0, 0.30, 3.75),
        "claude-sonnet-4-6": TokenRates(3.0, 15.0, 0.30, 3.75),
        "claude-opus-4-6": TokenRates(5.0, 25.0, 0.50, 6.25),
        "claude-opus-5": TokenRates(5.0, 25.0, 0.50, 6.25),
    },
}

# Rates with a KNOWN future change date, applied by _build_rates the moment
# ``date.today()`` reaches ``effective``. This keeps a published price change
# from depending on someone editing DEFAULT_RATES in the right month — the
# failure the module docstring calls out ("this table will drift"): the model
# stays priced=True, so the unpriced counter (the designed drift alarm) never
# fires for a stale-but-present entry. Entries are (provider, model,
# effective, rates); pure literals, stdlib date only.
_SCHEDULED_RATE_CHANGES: tuple = (
    # gemini-3.7-flash introductory pricing ends 2026-12-31; standard rate
    # from 2027-01-01 ($1.50 in / $7.50 out / $0.15 cache read per 1M).
    ("gemini", "gemini-3.7-flash", date(2027, 1, 1), TokenRates(1.50, 7.50, 0.15, 0.0)),
)

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


def _build_rates(today: date | None = None) -> dict[str, dict[str, TokenRates]]:
    today = today or date.today()
    rates = {p: dict(models) for p, models in DEFAULT_RATES.items()}
    # Scheduled changes apply over the defaults, operator overrides over both
    # — an operator pin always wins, before and after a scheduled date.
    for provider, model, effective, new_rates in _SCHEDULED_RATE_CHANGES:
        if today >= effective:
            rates.setdefault(provider, {})[model] = new_rates
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
    # Substring match handles provider-prefixed or dated model ids. LONGEST
    # matching key wins, so a specific key ("gemini-3.5-flash-lite",
    # "deepseek-v4-flash") beats the shorter key it contains
    # ("gemini-3.5-flash", "deepseek") regardless of dict order — which no
    # ordering discipline could guarantee anyway: LLM_PRICING_OVERRIDES merges
    # via dict.update, appending operator keys after every built-in.
    best_key = None
    best_rates = None
    for key, rates in table.items():
        if key and key in model_l and (best_key is None or len(key) > len(best_key)):
            best_key = key
            best_rates = rates
    return best_rates


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
