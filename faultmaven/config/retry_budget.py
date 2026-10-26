"""Are this deployment's two LLM retry ladders able to finish inside one turn?

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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from faultmaven.core.investigation.llm_error_handler import RetryConfig
    from faultmaven.core.investigation.turn_budget import LadderPlan


@dataclass(frozen=True)
class RetryLadderBudget:
    """Both ladders the engine runs, each modelled against one turn (#1294):
    a hung or 504-answering provider's (``2T + 2`` shipped), and a non-504
    5xx's priced at the full ``T`` per attempt (``3T + 14``, which bounds every
    ladder, a mixed one included)."""

    deadline_shaped: "LadderPlan"
    status_shaped: "LadderPlan"

    @property
    def fits(self) -> bool:
        return self.deadline_shaped.fits and self.status_shaped.fits


def model_retry_ladders(
    *,
    agent_timeout: float,
    attempt_seconds: float,
    config: Optional["RetryConfig"] = None,
) -> RetryLadderBudget:
    """Both ladders for one turn ceiling and one per-attempt ``T``.

    Every count comes from ``RetryConfig``, the object the running ladder
    reads, so the report and the runtime cannot disagree about a ladder's
    length. The breaker threshold is composed in rather than assumed: attempts
    that reach a provider are ``min(retries + 1, circuit_breaker_threshold)``,
    so the status-shaped ladder pays 3 attempts and all three backoffs
    (``3T + 14``), while the deadline-shaped one stops at its own cap, under the
    breaker (``2T + 2``).
    """
    from faultmaven.core.investigation.llm_error_handler import (
        LLMErrorHandler,
        RetryConfig,
    )
    from faultmaven.core.investigation.turn_budget import worst_case_ladder_plan
    from faultmaven.infrastructure.llm.router import LLM_CIRCUIT_BREAKER_THRESHOLD

    config = config or RetryConfig()
    # One handler, not one per backoff: ``calculate_delay`` is a pure function
    # of the config, and the instance exists only to reach it.
    schedule = LLMErrorHandler(config)

    def plan(retries: int) -> "LadderPlan":
        return worst_case_ladder_plan(
            agent_timeout=agent_timeout,
            attempt_seconds=attempt_seconds,
            paid_attempts=min(retries + 1, LLM_CIRCUIT_BREAKER_THRESHOLD),
            backoffs=[schedule.calculate_delay(n) for n in range(retries)],
        )

    return RetryLadderBudget(
        deadline_shaped=plan(min(config.max_deadline_retries, config.max_retries)),
        status_shaped=plan(config.max_retries),
    )


def describe_retry_ladder_budget(settings) -> RetryLadderBudget:
    """Model both retry ladders for *settings*' resolved chat provider: its
    turn ceiling, and its per-call ``T`` (``max(LLM_REQUEST_TIMEOUT,
    LLM_PROVIDER_TIMEOUT_OVERRIDES[provider])``)."""
    from faultmaven.config.turn_ceiling import resolve_turn_ceiling
    from faultmaven.infrastructure.llm.router import resolve_request_timeout

    return model_retry_ladders(
        agent_timeout=resolve_turn_ceiling(settings).ceiling_seconds,
        attempt_seconds=resolve_request_timeout(settings),
    )
