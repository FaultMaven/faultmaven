"""Central LLM spend metering.

Every billed provider API call funnels through :func:`record_provider_call`,
which is invoked once per real ``provider.generate()`` at the routing chokepoint
(``registry.route_request``) — *including* fallback / low-confidence attempts the
caller ends up discarding. That is the whole point: the fallback multiplier is
the single most likely cause of a token-cost spike, and metering only the winning
response (as a naive router-level hook would) makes it invisible.

Responsibilities, all best-effort (a metering failure must never break a call):

* Emit fine-grained Prometheus counters: tokens by type, USD cost, per-call
  count by disposition, and an unpriced-call counter so cost under-reporting is
  visible rather than silent.
* Accumulate per-turn spend into the :class:`TurnTokenTracker` bound to the
  current async context (set by the investigation engine around a turn).
* Emit a structured ``llm_call`` log line for per-call forensics.

Token buckets are DISJOINT (``input`` excludes cached prompt tokens; those live
in ``cache_read``) so summing them is correct and matches the cost computation.
Providers are responsible for normalizing to that shape.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from faultmaven.infrastructure.llm.pricing import estimate_cost_usd
from faultmaven.infrastructure.shims import (
    llm_call_tokens,
    llm_cost_usd,
    llm_provider_calls,
    llm_unpriced_calls,
)

logger = logging.getLogger(__name__)

# Bound by the investigation engine for the duration of a single turn, so every
# LLM call made while handling that turn (main generation, tool loop, KB Q&A,
# classifier, synthesis, and any fallback attempts) accrues to one tracker.
active_token_tracker: ContextVar[TurnTokenTracker | None] = ContextVar(
    "active_token_tracker", default=None
)


@dataclass
class TurnTokenTracker:
    """Accumulates true spend for one investigation turn.

    Buckets are disjoint, so ``total_tokens`` is a correct sum. ``cost_usd`` is
    accumulated from the same price table used by the Prometheus cost metric, so
    the per-turn log and the dashboards agree. ``total_calls`` counts every
    billed provider call in the turn — a call-amplification signal on its own.

    ``add`` is NOT idempotent: every billed call must be fed exactly once. The
    metering architecture guarantees this — each provider call is metered at the
    single registry chokepoint, or (for a dedicated concrete DA provider that
    bypasses the registry) at that one direct call site — never both. We
    deliberately do not de-dupe by ``id(response)``: short-lived response objects
    can share an id after garbage collection, which would silently drop counts.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    total_calls: int = 0
    unpriced_calls: int = 0

    def add(self, response: Any, cost_usd: float = 0.0, priced: bool = True) -> None:
        if response is None:
            return
        self.input_tokens += int(getattr(response, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(response, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(getattr(response, "cache_read_tokens", 0) or 0)
        self.cache_write_tokens += int(getattr(response, "cache_write_tokens", 0) or 0)
        self.cost_usd += cost_usd
        self.total_calls += 1
        if not priced:
            self.unpriced_calls += 1

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


def record_provider_call(
    provider: str,
    model: str,
    response: Any,
    latency_ms: float,
    *,
    outcome: str = "kept",
) -> None:
    """Meter one real, billed provider API call. Best-effort; never raises.

    Args:
        provider: Provider name (e.g. "anthropic"). Bounded label.
        model: Model id actually used. Bounded-ish label.
        response: The ``LLMResponse`` returned by ``provider.generate()``.
        latency_ms: Wall-clock latency of the call.
        outcome: "kept" (returned to caller) or "low_confidence" (billed then
            discarded by the fallback chain — pure waste worth alerting on).
    """
    try:
        provider = provider or "unknown"
        model = model or getattr(response, "model", None) or "unknown"

        input_tokens = int(getattr(response, "input_tokens", 0) or 0)
        output_tokens = int(getattr(response, "output_tokens", 0) or 0)
        cache_read = int(getattr(response, "cache_read_tokens", 0) or 0)
        cache_write = int(getattr(response, "cache_write_tokens", 0) or 0)

        cost_usd, priced = estimate_cost_usd(
            provider,
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

        # Prometheus — bounded labels only (never case_id/user_id/etc.).
        llm_provider_calls.labels(provider=provider, model=model, outcome=outcome).inc()
        for token_type, amount in (
            ("input", input_tokens),
            ("output", output_tokens),
            ("cache_read", cache_read),
            ("cache_write", cache_write),
        ):
            if amount:
                llm_call_tokens.labels(
                    provider=provider, model=model, token_type=token_type
                ).inc(amount)
        if priced:
            if cost_usd:
                llm_cost_usd.labels(provider=provider, model=model).inc(cost_usd)
        else:
            llm_unpriced_calls.labels(provider=provider, model=model).inc()

        # Per-turn accumulation.
        tracker = active_token_tracker.get()
        if tracker is not None:
            tracker.add(response, cost_usd=cost_usd, priced=priced)

        # Per-call structured forensics.
        logger.info(
            "llm_call",
            extra={
                "provider": provider,
                "model": model,
                "outcome": outcome,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "total_tokens": int(getattr(response, "tokens_used", 0) or 0),
                "prompt_cache_hit": bool(getattr(response, "prompt_cache_hit", False)),
                "estimated_cost_usd": round(cost_usd, 6),
                "cost_priced": priced,
                "latency_ms": round(latency_ms, 1),
            },
        )
    except Exception as exc:  # metering must never break a request
        logger.warning("record_provider_call failed (non-fatal): %s", exc)
