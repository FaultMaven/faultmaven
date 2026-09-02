"""The turn deadline, and the arithmetic that budgets the retry ladder against it.

Two independently-configured timeouts decide whether an LLM retry ladder can
finish inside one turn, and nothing related them (#1278, #1292):

* ``LLMSettings.request_timeout`` / ``LLM_PROVIDER_TIMEOUT_OVERRIDES`` — what one
  attempt costs against a hung provider.
* ``AgentSettings.agent_request_timeout`` / ``AGENT_PROVIDER_TIMEOUT_OVERRIDES``
  — the turn-wide ``asyncio.wait_for`` ceiling that cancels whatever is running.

The behavioural half of the fix lives in
``tests/integration/core/test_llm_ladder_turn_budget.py``, which drives the real
ladder. This file pins the ARITHMETIC, at REAL configured values rather than at
the scaled-down ones a wall-clock test can afford — because the numbers are
where the reasoning went wrong before. #1292 stated the ladder cost as
``3T + 2 + 4`` and concluded ``LLM_REQUEST_TIMEOUT <= 38`` was safe at a 120s
turn. It dropped the third backoff: the eight seconds ARE spent, before the
fourth iteration that the router's circuit breaker short-circuits. The cost is
``3T + 14`` and the real ceiling is 35 — so a guard written to that issue's table
would have certified a breaching configuration as safe.
"""

import time

import pytest

from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.turn_budget import (
    TURN_BUDGET_BACKSTOP_RESERVE_SECONDS,
    TURN_BUDGET_RESERVE_SECONDS,
    backstop_turn_budget,
    bind_turn_deadline,
    can_afford_next_attempt,
    clamp_to_turn_budget,
    remaining_turn_budget,
    spendable_turn_budget,
    worst_case_ladder_plan,
)
from faultmaven.infrastructure.llm.router import LLM_CIRCUIT_BREAKER_THRESHOLD

# The shipped ladder shape, SOURCED rather than restated — read from the two
# modules production reads them from (``RetryConfig(max_retries=3,
# base_delay_seconds=2.0, exponential_base=2.0)`` and the router's
# ``circuit_breaker_threshold=3``), so a change to either lands here as a
# failure instead of drifting silently past a table of literals.
_CONFIG = RetryConfig()
_BACKOFFS = [
    LLMErrorHandler(_CONFIG).calculate_delay(n) for n in range(_CONFIG.max_retries)
]
_PAID_ATTEMPTS = min(_CONFIG.max_retries + 1, LLM_CIRCUIT_BREAKER_THRESHOLD)


def _plan(request_timeout: float, agent_timeout: float):
    return worst_case_ladder_plan(
        agent_timeout=agent_timeout,
        attempt_seconds=request_timeout,
        paid_attempts=_PAID_ATTEMPTS,
        backoffs=_BACKOFFS,
    )


@pytest.mark.unit
class TestTheShippedLadderShape:
    """The constants the arithmetic below is built on, asserted not assumed."""

    def test_backoff_schedule_is_two_four_eight(self):
        assert _BACKOFFS == [2.0, 4.0, 8.0]

    def test_only_three_attempts_ever_reach_a_provider(self):
        """``max_retries=3`` means FOUR iterations, but the breaker opens at 3.

        This is the correction at the heart of #1292's wrong arithmetic. The
        fourth iteration still pays its 8s backoff and then never contacts a
        provider, so the ladder costs three request timeouts and all fourteen
        seconds of backoff — not two backoffs, and not four timeouts.
        """
        assert _CONFIG.max_retries + 1 == 4
        assert LLM_CIRCUIT_BREAKER_THRESHOLD == 3
        assert _PAID_ATTEMPTS == 3
        assert _plan(request_timeout=10, agent_timeout=600).full_ladder_seconds == (
            3 * 10 + 14
        )


# (id, LLM_REQUEST_TIMEOUT, AGENT_REQUEST_TIMEOUT, full ladder cost, fits, attempts afforded)
#
# A POPULATION, not a fixture: a single (T, A) pair proves nothing about a
# boundary, and the boundary is where the previous analysis went wrong. Spans
# both sides of 35/36 at the default 120s turn, the shipped default, the two
# configurations measured on real deployments, and the values the documentation
# itself invites (`{"fireworks": 180, "ollama": 600}`).
_LADDER_POPULATION = [
    # ---- fits ----
    ("shipped_default_30_120", 30, 120, 104.0, True, 3),
    ("boundary_fits_35_120", 35, 120, 119.0, True, 3),
    ("generous_turn_30_600", 30, 600, 104.0, True, 3),
    ("tiny_timeout_5_120", 5, 120, 29.0, True, 3),
    # ---- breaches ----
    # One second past the ceiling. #1292 called 38 safe here; it is not, and 36
    # is already over.
    ("boundary_breaches_36_120", 36, 120, 122.0, False, 3),
    # The value #1292 proposed as the safe ceiling. It breaches by 8s AND the
    # deadline-aware ladder can only afford two of the three attempts.
    ("issue_1292_claimed_safe_38_120", 38, 120, 128.0, False, 2),
    # This workspace's own .env at the time #1292 was filed.
    ("dev_env_90_120", 90, 120, 284.0, False, 1),
    # Live cluster: LLM_PROVIDER_TIMEOUT_OVERRIDES gemini=120,
    # AGENT_PROVIDER_TIMEOUT_OVERRIDES gemini=240. Breaches by 134s.
    ("production_120_240", 120, 240, 374.0, False, 1),
    # A single attempt alone exceeds the whole turn — only clamping saves this.
    ("attempt_exceeds_turn_180_120", 180, 120, 554.0, False, 1),
    ("documented_ollama_600_120", 600, 120, 1814.0, False, 1),
]


@pytest.mark.unit
class TestWorstCaseLadderPlan:
    @pytest.mark.parametrize(
        "request_timeout,agent_timeout,full_seconds,fits,attempts",
        [pytest.param(*row[1:], id=row[0]) for row in _LADDER_POPULATION],
    )
    def test_population(
        self, request_timeout, agent_timeout, full_seconds, fits, attempts
    ):
        plan = _plan(request_timeout, agent_timeout)
        assert plan.full_ladder_seconds == full_seconds
        assert plan.fits is fits
        assert plan.attempts == attempts

    def test_the_population_actually_spans_both_verdicts(self):
        """Guard against a table that has quietly become all-fits or all-breach.

        A population pin whose rows all agree is a single fixture wearing a
        parametrize decorator.
        """
        verdicts = {_plan(row[1], row[2]).fits for row in _LADDER_POPULATION}
        assert verdicts == {True, False}
        afforded = {_plan(row[1], row[2]).attempts for row in _LADDER_POPULATION}
        assert afforded == {1, 2, 3}

    @pytest.mark.parametrize(
        "request_timeout,agent_timeout",
        [(row[1], row[2]) for row in _LADDER_POPULATION],
    )
    def test_the_plan_never_proposes_to_overrun_the_turn(
        self, request_timeout, agent_timeout
    ):
        """The invariant, over the WHOLE population rather than per row.

        Whatever the two timeouts are, what the ladder is allowed to spend stays
        inside the turn budget. This is the property the per-row numbers are
        evidence for; stating it separately means a future row cannot be added
        with a value that violates it.
        """
        plan = _plan(request_timeout, agent_timeout)
        assert plan.afforded_seconds <= agent_timeout - TURN_BUDGET_RESERVE_SECONDS
        assert 1 <= plan.attempts <= _PAID_ATTEMPTS

    def test_fits_is_exactly_whole_ladder_plus_reserve_inside_the_turn(self):
        """``fits`` must not quietly become "the afforded attempts fit".

        Those differ: at T=36/A=120 the deadline-aware ladder affords all three
        attempts (114s of a 119s budget) and yet the CONFIGURATION does not fit,
        because the full ladder costs 122s. Reporting True there would tell an
        operator their timeouts are coherent when they are not.
        """
        plan = _plan(request_timeout=36, agent_timeout=120)
        assert plan.attempts == _PAID_ATTEMPTS  # every attempt afforded ...
        assert plan.fits is False  # ... and still incoherent

    def test_no_attempt_at_all_when_the_budget_is_already_gone(self):
        plan = _plan(request_timeout=30, agent_timeout=TURN_BUDGET_RESERVE_SECONDS)
        assert plan.attempts == 0
        assert plan.afforded_seconds == 0.0
        assert plan.fits is False


@pytest.mark.unit
class TestCanAffordNextAttempt:
    """Read adversarially: what satisfies this check while violating its intent?"""

    def test_no_deadline_means_everything_fits(self):
        """The pre-existing behaviour of every caller outside a bound turn."""
        assert can_afford_next_attempt(None, backoff_seconds=8.0, attempt_seconds=600.0)

    def test_exactly_enough_is_enough(self):
        assert can_afford_next_attempt(10.0, backoff_seconds=2.0, attempt_seconds=8.0)

    def test_one_millisecond_short_is_refused(self):
        assert not can_afford_next_attempt(
            9.999, backoff_seconds=2.0, attempt_seconds=8.0
        )

    def test_negative_budget_is_refused(self):
        assert not can_afford_next_attempt(
            -0.5, backoff_seconds=0.0, attempt_seconds=0.0
        )

    def test_a_zero_cost_estimate_still_guards_the_backoff(self):
        """The estimate may be 0.0 (an instant first failure, or a caller with
        nothing to offer). The backoff is spent regardless, so it must still be
        checked — otherwise a ladder with 1s left would sleep 8s past the
        deadline before discovering it had none."""
        assert not can_afford_next_attempt(
            1.0, backoff_seconds=8.0, attempt_seconds=0.0
        )
        assert can_afford_next_attempt(8.0, backoff_seconds=8.0, attempt_seconds=0.0)


@pytest.mark.unit
class TestBindingTheDeadline:
    def test_unbound_reads_none_not_zero(self):
        """``None`` and ``0`` are opposite answers.

        ``None`` means "no ceiling" and every check passes; ``0`` would mean
        "no time left" and every check fails. A background job or a direct-call
        test must get the first.
        """
        assert remaining_turn_budget() is None
        assert spendable_turn_budget() is None

    def test_bound_reads_close_to_the_ceiling(self):
        with bind_turn_deadline(120):
            remaining = remaining_turn_budget()
        assert remaining is not None
        assert 119.0 < remaining <= 120.0

    def test_the_reserve_is_deducted_once(self):
        with bind_turn_deadline(120):
            remaining = remaining_turn_budget()
            spendable = spendable_turn_budget()
        assert remaining is not None and spendable is not None
        assert abs((remaining - spendable) - TURN_BUDGET_RESERVE_SECONDS) < 0.05

    def test_the_deadline_does_not_outlive_its_block(self):
        """Otherwise the next turn on this worker inherits a spent budget."""
        with bind_turn_deadline(120):
            assert remaining_turn_budget() is not None
        assert remaining_turn_budget() is None

    def test_binding_none_leaves_an_enclosing_deadline_alone(self):
        with bind_turn_deadline(120):
            with bind_turn_deadline(None):
                assert remaining_turn_budget() is not None
            assert remaining_turn_budget() is not None

    def test_a_passed_deadline_reports_negative_rather_than_clamping(self):
        with bind_turn_deadline(-0.5):
            remaining = remaining_turn_budget()
        assert remaining is not None and remaining < 0

    def test_the_deadline_is_monotonic_not_wall_clock(self):
        """A duration, measured against a clock that cannot step backwards."""
        before = time.monotonic()
        with bind_turn_deadline(60):
            remaining = remaining_turn_budget()
        after = time.monotonic()
        assert remaining is not None
        assert 60 - (after - before) <= remaining <= 60


@pytest.mark.unit
class TestClampingOneCallsOwnTimeout:
    """``clamp_to_turn_budget`` is what bounds EVERY router-borne LLM call.

    It is applied at ``LLMRouter._resolve_timeout``, the single place such a
    call learns its ceiling — so intent classification and KB/document answer
    synthesis are bounded by it too, not only the calls the investigation
    engine wraps in a retry ladder (#1278's "each subsequent call in the turn").
    """

    def test_unbound_returns_the_configured_ceiling_untouched(self):
        assert clamp_to_turn_budget(600.0) == 600.0

    def test_a_ceiling_that_fits_is_untouched(self):
        with bind_turn_deadline(120):
            assert clamp_to_turn_budget(30.0) == 30.0

    def test_a_ceiling_larger_than_the_turn_is_cut_to_the_budget(self):
        with bind_turn_deadline(30):
            clamped = clamp_to_turn_budget(600.0)
        assert clamped < 30.0
        assert clamped == pytest.approx(30.0 - TURN_BUDGET_RESERVE_SECONDS, abs=0.05)

    def test_a_spent_budget_clamps_to_zero_never_negative(self):
        """``asyncio.wait_for`` turns 0.0 into an immediate, CLASSIFIED timeout.

        A negative would too, but zero is the honest floor and keeps the value
        printable in a log line without a sign that reads like a bug.
        """
        with bind_turn_deadline(-5):
            assert clamp_to_turn_budget(600.0) == 0.0


@pytest.mark.unit
class TestTheProviderClampWinsTheRace:
    """Two clamps bound an attempt, and WHICH one fires decides whether the
    circuit breaker ever learns the provider is down.

    The provider's own timeout (inside ``call_external``) raises
    ``ExternalCallTimeout`` from an ``except asyncio.TimeoutError`` handler and
    records a breaker failure. The ladder's outer backstop cancels the
    coroutine instead, and ``CancelledError`` is a ``BaseException`` that none
    of that method's handlers catch — nothing is recorded, the breaker never
    opens, and the outage repeats at full cost every turn.

    So the provider clamp must expire STRICTLY EARLIER. That is not luck: it
    follows from the backstop reserve being the smaller of the two, and it is
    asserted here rather than left as a comment.
    """

    def test_the_backstop_reserve_is_the_smaller_one(self):
        assert TURN_BUDGET_BACKSTOP_RESERVE_SECONDS < TURN_BUDGET_RESERVE_SECONDS

    def test_the_backstop_deadline_is_later_than_the_provider_clamp(self):
        with bind_turn_deadline(120):
            provider = spendable_turn_budget()
            backstop = backstop_turn_budget()
        assert provider is not None and backstop is not None
        assert backstop > provider

    def test_the_gap_is_the_difference_between_the_two_reserves(self):
        with bind_turn_deadline(120):
            gap = backstop_turn_budget() - spendable_turn_budget()
        expected = TURN_BUDGET_RESERVE_SECONDS - TURN_BUDGET_BACKSTOP_RESERVE_SECONDS
        assert gap == pytest.approx(expected, abs=0.05)

    def test_the_backstop_still_lands_inside_the_turn(self):
        """Later than the provider clamp, but never at or past the deadline the
        route will cancel on — or it would be the 504 it exists to prevent."""
        with bind_turn_deadline(120):
            assert backstop_turn_budget() < remaining_turn_budget()

    def test_unbound_reads_none_like_every_other_budget_reader(self):
        assert backstop_turn_budget() is None
