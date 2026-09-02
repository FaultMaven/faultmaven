"""Is this deployment's LLM retry ladder able to finish inside one turn?

A configuration-coherence report, sibling to the startup gates in
``investigation_capability`` — same question shape ("can the engine actually do
what this configuration asks of it"), different axis.

Two independently-configured timeouts decide the answer, and nothing else in the
codebase relates them (#1292):

* ``LLMSettings.request_timeout`` / ``LLM_PROVIDER_TIMEOUT_OVERRIDES`` — what one
  attempt against a hung provider costs.
* ``AgentSettings.agent_request_timeout`` / ``AGENT_PROVIDER_TIMEOUT_OVERRIDES``
  — the turn-wide ``asyncio.wait_for`` ceiling that cancels whatever is running.

The running ladder no longer BREAKS on an incoherent pair — it budgets against
the deadline and stops early with a classified error rather than being cancelled
mid-attempt (``core/investigation/turn_budget``) — so this is a **report, not a
boot gate**. What it tells an operator is how many provider attempts their two
numbers actually buy, which is not derivable from either one alone and which no
other signal in the system states.

Lives in the config layer, not beside the endpoint that serves it, because the
answer is composed from three things the API layer has no business knowing: the
engine's retry configuration, the router's circuit-breaker threshold, and the
two timeout maps. Composition of core and infrastructure belongs below the
route that reports it — the same reason ``investigation_capability`` sits here
rather than in ``main``. (``tests/unit/architecture/test_architecture_boundaries``
enforces that direction for ``faultmaven/api``; ``lint-imports`` does not, so
the test is the one that catches a regression.)

The imports are function-local for the reason ``llm_config_overrides`` does the
same: nothing here is needed to load settings, so keeping them out of module
scope leaves the config package importable without dragging in the LLM stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from faultmaven.core.investigation.turn_budget import LadderPlan


def describe_retry_ladder_budget(settings) -> "LadderPlan":
    """Model the retry ladder for *settings*' resolved chat provider.

    Composes the four numbers the model needs from the three places that own
    them — the retry configuration, the router's circuit-breaker threshold, and
    the two timeout maps — and hands them to the pure arithmetic in
    ``turn_budget.worst_case_ladder_plan``.

    The breaker threshold is part of the composition rather than an assumption
    inside the arithmetic: attempts that actually reach a provider are
    ``min(max_retries + 1, circuit_breaker_threshold)``, not ``max_retries + 1``,
    because the breaker opens on the third failure and short-circuits the fourth
    iteration. With the shipped values that is 3 paid attempts and all 4
    backoffs, so a hung provider costs ``3T + (2 + 4 + 8) = 3T + 14``.
    """
    from faultmaven.core.investigation.llm_error_handler import (
        LLMErrorHandler,
        RetryConfig,
    )
    from faultmaven.core.investigation.turn_budget import worst_case_ladder_plan
    from faultmaven.infrastructure.llm.router import (
        LLM_CIRCUIT_BREAKER_THRESHOLD,
        resolve_chat_provider_name,
        resolve_request_timeout,
    )

    provider = resolve_chat_provider_name(settings)
    config = RetryConfig()
    # One handler, not one per backoff: ``calculate_delay`` is a pure function
    # of the config, and the instance exists only to reach it.
    schedule = LLMErrorHandler(config)
    backoffs = [schedule.calculate_delay(n) for n in range(config.max_retries)]
    return worst_case_ladder_plan(
        agent_timeout=float(settings.agent.timeout_for_provider(provider)),
        attempt_seconds=resolve_request_timeout(settings),
        paid_attempts=min(config.max_retries + 1, LLM_CIRCUIT_BREAKER_THRESHOLD),
        backoffs=backoffs,
    )
