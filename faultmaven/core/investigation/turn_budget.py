"""The turn-wide deadline, and the budget the LLM retry ladder spends against it.

A turn is a BOUNDED operation: ``submit_turn`` runs ``process_turn`` under an
``asyncio.wait_for`` whose ceiling is ``AGENT_REQUEST_TIMEOUT`` (resolved
per-provider). Inside that bound the engine runs an LLM retry ladder whose own
worst-case cost is a function of a DIFFERENT setting in a DIFFERENT settings
class — ``LLM_REQUEST_TIMEOUT`` / ``LLMSettings.provider_timeout_overrides``.
Nothing related the two, so the ladder could begin an attempt it had no room to
finish, and the turn-wide ``wait_for`` cancelled it part-way through
(#1278, #1292).

Being cancelled mid-ladder costs twice:

* The retry classification never runs, so the caller gets an opaque
  ``504 REQUEST_TIMEOUT`` instead of the honest ``503 LLM_PROVIDER_UNAVAILABLE``
  + ``Retry-After`` that the LLM error path already produces.
* The cancellation also stops the router's circuit breaker from accumulating
  failures at its normal rate, so the same full-budget failure repeats instead
  of fast-failing.

The invariant this module exists to hold is: **a bounded operation must not
begin a step it cannot finish inside its own bound.**

Two pieces, deliberately separate:

``bind_turn_deadline`` / ``remaining_turn_budget`` / ``spendable_turn_budget``
    The RUNTIME budget. The API layer binds the deadline where it applies the
    ``wait_for`` (the only place that knows both the ceiling and the instant it
    started); the retry ladder reads what is left. Carried in a
    :class:`~contextvars.ContextVar` for the same reason
    ``infrastructure.llm.metering.active_token_tracker`` is — every LLM call
    made while handling one turn is many frames below the binding site, and
    threading a deadline through each of them would be a signature change on
    every layer in between.

    **Unbound means unbounded.** ``remaining_turn_budget()`` returns ``None``
    outside a bound turn (direct-call tests, background jobs, the CLI), and
    every consumer treats ``None`` as "no ceiling", which is exactly the
    behaviour those paths had before. That is also why a guard here is only as
    good as its binding site, and why the binding is pinned by its own test.

``worst_case_ladder_plan``
    The static ARITHMETIC, for reporting a configuration rather than for
    running one. Answers "given these two independently-configured timeouts,
    can the ladder complete inside the turn, and how many provider attempts
    does the turn budget actually buy?" — the cross-check #1292 asked for. Pure
    and side-effect free so the answer can be pinned at real configured values
    rather than at scaled-down test ones.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

# The monotonic instant by which the current turn must be finished, or None when
# no turn-wide ceiling is in force. Monotonic (not wall clock) because the value
# is only ever used as a duration difference and must not move under an NTP step.
_turn_deadline: ContextVar[Optional[float]] = ContextVar("turn_deadline", default=None)


# Headroom deliberately left unspent at the end of the turn budget.
#
# The point of stopping early is to return a CLASSIFIED error, and building and
# returning it is not free: the ladder's ErrorResult becomes a
# MilestoneEngineError, unwinds through the engine and the turn service, and is
# mapped at the API boundary. Spending the budget down to the last millisecond
# would hand that work to the very ``wait_for`` we are trying to beat, and the
# caller would get the opaque 504 anyway.
#
# One second, not more: the reserve is pure loss on the healthy path, where it
# is the window in which a response that WOULD have arrived is cut short
# instead. That window is nearly worthless in practice — a turn whose LLM call
# only returns with under a second of budget left has no room for the
# post-processing that follows it and was going to be cancelled regardless — but
# it is not zero, so the constant is kept small and named rather than inlined.
TURN_BUDGET_RESERVE_SECONDS = 1.0


@contextmanager
def bind_turn_deadline(seconds: Optional[float]) -> Iterator[None]:
    """Bind a turn-wide deadline ``seconds`` from now, for the enclosed block.

    Bind this at the site that applies the turn-wide ``asyncio.wait_for`` and
    nowhere else: that site is the only one that knows both the ceiling and the
    instant the clock started, and deriving either independently re-creates the
    drift this module exists to remove.

    The deadline is computed BEFORE the ``wait_for`` starts, so it is a hair
    earlier than the instant the cancellation actually fires. That direction is
    deliberate — the ladder's view of the budget must never be more optimistic
    than the ceiling that will cancel it.

    ``seconds=None`` binds nothing and leaves any enclosing deadline in force,
    so a caller with no ceiling of its own does not silently clear one.
    """
    if seconds is None:
        yield
        return
    token = _turn_deadline.set(time.monotonic() + float(seconds))
    try:
        yield
    finally:
        _turn_deadline.reset(token)


def remaining_turn_budget() -> Optional[float]:
    """Seconds until the current turn's deadline, or ``None`` when unbound.

    May be negative: the deadline can pass while a caller is mid-decision, and
    reporting that honestly is more useful than clamping it to zero.
    """
    deadline = _turn_deadline.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def spendable_turn_budget(reserve: Optional[float] = None) -> Optional[float]:
    """What is left of the turn that a step may actually spend, or ``None``.

    ``remaining_turn_budget()`` minus the reserve. Apply the reserve here and
    only here — subtracting it a second time downstream would compound into a
    ceiling nobody configured.

    ``reserve=None`` reads ``TURN_BUDGET_RESERVE_SECONDS`` at CALL time rather
    than binding it as a default argument at import time. The difference is not
    cosmetic: a default argument freezes the value, so a test that scales the
    whole ladder down to sub-second timings cannot scale the reserve with it and
    silently measures a budget of zero — a ladder that refuses every attempt,
    which looks like the guard working and is not.
    """
    remaining = remaining_turn_budget()
    if remaining is None:
        return None
    if reserve is None:
        reserve = TURN_BUDGET_RESERVE_SECONDS
    return remaining - reserve


def can_afford_next_attempt(
    spendable: Optional[float],
    backoff_seconds: float,
    attempt_seconds: float,
) -> bool:
    """Whether one backoff plus one attempt of ``attempt_seconds`` still fits.

    ``spendable is None`` means no turn-wide ceiling is in force, so everything
    fits — that is what preserves the pre-existing behaviour of every caller
    outside a bound turn.

    ``attempt_seconds`` is an ESTIMATE and is allowed to be wrong: it is what
    keeps the ladder from spending its whole budget on attempts it can already
    tell will not complete. It is not what makes the invariant hold — clamping
    each attempt to the spendable budget is (see ``LLMErrorHandler.with_retry``),
    which is why an under-estimate degrades the ladder's efficiency rather than
    its correctness.
    """
    if spendable is None:
        return True
    return spendable >= backoff_seconds + attempt_seconds


@dataclass(frozen=True)
class LadderPlan:
    """What a turn budget buys from the retry ladder at a given attempt cost."""

    attempts: int
    """Provider-contacting attempts the turn budget affords. Normally >= 1 — a
    turn always makes one attempt, clamped to whatever budget it has — and 0
    only when there is no budget left to clamp to."""

    paid_attempts: int
    """Provider-contacting attempts the ladder would make with unlimited
    budget."""

    afforded_seconds: float
    """Wall clock the afforded attempts and the backoffs between them cost."""

    full_ladder_seconds: float
    """Wall clock the whole ladder costs — every paid attempt plus EVERY
    backoff, including the one before the attempt the circuit breaker
    short-circuits."""

    fits: bool
    """Whether the whole ladder completes inside the turn budget. False means
    the two timeouts are configured incoherently: the deadline-aware ladder
    keeps the turn honest by cutting attempts short, but the operator is
    getting fewer retries than the retry configuration says."""


def worst_case_ladder_plan(
    *,
    agent_timeout: float,
    attempt_seconds: float,
    paid_attempts: int,
    backoffs: Sequence[float],
    reserve: Optional[float] = None,
) -> LadderPlan:
    """Model the retry ladder against a turn budget, for reporting a config.

    ``attempt_seconds`` is the WORST-CASE cost of one attempt — the LLM request
    timeout for the resolved provider — because the failure this budgets for is
    a provider that hangs. A provider that fails fast costs a fraction of it and
    is never the configuration that breaches.

    ``paid_attempts`` is how many attempts actually reach a provider, which is
    NOT ``max_retries + 1``: the router's circuit breaker opens at its own
    threshold and short-circuits the rest of the ladder in microseconds. With
    the shipped ``max_retries=3`` and ``circuit_breaker_threshold=3`` that is 3
    paid attempts and 4 backoff-separated iterations, so the full ladder costs
    ``3T + (2 + 4 + 8) = 3T + 14`` — the eight seconds ARE spent, before the
    attempt the breaker refuses. Callers pass both numbers rather than having
    them assumed here, so the coupling to the breaker threshold is visible at
    the call site instead of hidden in this arithmetic.

    Returns the plan the deadline-aware ladder would follow, using the same
    ``can_afford_next_attempt`` predicate the running ladder uses, so the report
    and the runtime cannot drift apart.
    """
    if reserve is None:
        reserve = TURN_BUDGET_RESERVE_SECONDS
    spendable = agent_timeout - reserve

    full = float(attempt_seconds) * paid_attempts + float(sum(backoffs))

    if spendable <= 0:
        # No room even to begin. Unreachable from configuration alone —
        # ``agent_request_timeout`` is constrained ``ge=30`` — but modelled
        # rather than assumed away, because the running ladder refuses the same
        # way when an EARLIER ladder in the same turn has already spent the
        # budget, and a report that claimed one attempt there would be wrong.
        return LadderPlan(
            attempts=0,
            paid_attempts=paid_attempts,
            afforded_seconds=0.0,
            full_ladder_seconds=full,
            fits=False,
        )

    # The first attempt is otherwise never refused. Refusing it would fail a
    # turn against a perfectly healthy provider the moment a single request
    # timeout exceeds the turn budget, which is a configuration to warn about,
    # not one to break. It is clamped instead (``with_retry`` clamps for real).
    attempts = 1
    spent = min(attempt_seconds, spendable)

    for backoff in backoffs:
        if attempts >= paid_attempts:
            # Beyond this point the breaker answers instead of a provider, so
            # the remaining iterations cost their backoff and nothing else.
            break
        if not can_afford_next_attempt(spendable - spent, backoff, attempt_seconds):
            break
        spent += backoff + attempt_seconds
        attempts += 1

    return LadderPlan(
        attempts=attempts,
        paid_attempts=paid_attempts,
        afforded_seconds=spent,
        full_ladder_seconds=full,
        fits=full + reserve <= agent_timeout,
    )
