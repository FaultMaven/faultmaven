"""The LLM retry ladder, run against a turn-wide deadline that will cancel it.

Every test here is a CONTROL/TREATMENT pair over the same failing provider: the
same ladder, the same timings, run once with no deadline bound (what the code
did before #1278/#1292) and once with one bound (what it does now). The control
arm is the fails-before evidence in situ — it is the mid-ladder cancellation
these issues are about, and it is asserted, not asserted-about.

Timings are SCALED. The real numbers (``LLM_REQUEST_TIMEOUT`` against
``AGENT_REQUEST_TIMEOUT``, 35/36 at the 120s boundary, 120/240 in the cluster)
are pinned exactly and without a clock in
``tests/unit/core/investigation/test_turn_budget.py``; a wall-clock test can only
afford the shapes, so it takes the shapes and leaves generous margins. The
reserve is scaled with everything else — leaving it at its production 1.0s
against a 2s turn would make the budget zero, the ladder refuse every attempt,
and the whole file pass for the wrong reason.
"""

import asyncio
import time

import pytest

from faultmaven.core.investigation import turn_budget
from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.turn_budget import bind_turn_deadline
from faultmaven.exceptions import TURN_BUDGET_EXHAUSTED, ExternalCallTimeout

# Scaled reserve: big enough that the ladder's early stop is unambiguously
# earlier than the cancellation even on a loaded runner (every assertion below
# has at least half a second of margin), small enough to leave most of the turn
# usable.
SCALED_RESERVE = 0.5

CANCELLED = "cancelled"


@pytest.fixture(autouse=True)
def scaled_reserve(monkeypatch):
    monkeypatch.setattr(turn_budget, "TURN_BUDGET_RESERVE_SECONDS", SCALED_RESERVE)


class _Outcome:
    def __init__(self, elapsed, attempts, error_code):
        self.elapsed = elapsed
        self.attempts = attempts
        self.error_code = error_code

    def __repr__(self):  # pragma: no cover - failure output only
        return (
            f"<Outcome {self.error_code} after {self.elapsed:.2f}s, "
            f"{self.attempts} attempt(s)>"
        )


async def _run_turn(
    *,
    hang_seconds,
    turn_seconds,
    base_delay,
    bind,
    ladders=1,
):
    """One 'turn': the ladder(s), under the turn-wide wait_for the route applies.

    ``bind`` is the whole treatment: whether the deadline the ``wait_for`` will
    enforce is also visible to the ladder inside it.
    """
    attempts = {"n": 0}

    async def hanging_provider():
        attempts["n"] += 1
        await asyncio.sleep(hang_seconds)
        raise ExternalCallTimeout(
            f"External call to LLM_Providers.generate timed out after {hang_seconds}s",
            service="LLM_Providers",
            operation="generate",
            timeout=hang_seconds,
        )

    async def body():
        last = None
        for _ in range(ladders):
            handler = LLMErrorHandler(RetryConfig(base_delay_seconds=base_delay))
            _, last = await handler.with_retry(operation=hanging_provider)
        return last

    started = time.monotonic()
    try:
        if bind:
            with bind_turn_deadline(turn_seconds):
                error = await asyncio.wait_for(body(), timeout=turn_seconds)
        else:
            error = await asyncio.wait_for(body(), timeout=turn_seconds)
        code = error.error_code if error is not None else None
    except asyncio.TimeoutError:
        code = CANCELLED
    return _Outcome(time.monotonic() - started, attempts["n"], code)


@pytest.mark.integration
@pytest.mark.asyncio
class TestOneAttemptCostsMoreThanTheWholeTurn:
    """``LLM_REQUEST_TIMEOUT`` above ``AGENT_REQUEST_TIMEOUT``.

    The shape only CLAMPING can fix. A ladder that merely refuses retries still
    begins its first attempt, and here the first attempt alone outlives the
    turn — so the turn is cancelled inside it and the caller gets the opaque 504
    whatever the retry policy says.
    """

    PARAMS = dict(hang_seconds=5.0, turn_seconds=2.0, base_delay=0.05)

    async def test_control_the_turn_is_cancelled_mid_attempt(self):
        outcome = await _run_turn(**self.PARAMS, bind=False)
        assert outcome.error_code == CANCELLED
        assert outcome.attempts == 1

    async def test_treatment_the_attempt_is_clamped_and_classified(self):
        outcome = await _run_turn(**self.PARAMS, bind=True)
        assert outcome.error_code == TURN_BUDGET_EXHAUSTED
        assert outcome.attempts == 1
        assert outcome.elapsed < self.PARAMS["turn_seconds"]


@pytest.mark.integration
@pytest.mark.asyncio
class TestTheLadderOverrunsTheTurn:
    """Each attempt fits; the ladder as a whole does not.

    This is the production shape: 3x120s + 14s of backoff against a 240s turn.
    """

    PARAMS = dict(hang_seconds=0.5, turn_seconds=2.0, base_delay=0.2)

    async def test_control_the_turn_is_cancelled_part_way_down_the_ladder(self):
        outcome = await _run_turn(**self.PARAMS, bind=False)
        assert outcome.error_code == CANCELLED
        # Cancelled with attempts still owed: the classification never ran.
        assert 1 <= outcome.attempts < 4

    async def test_treatment_stops_early_with_a_classified_error(self):
        outcome = await _run_turn(**self.PARAMS, bind=True)
        assert outcome.error_code == TURN_BUDGET_EXHAUSTED
        assert outcome.elapsed < self.PARAMS["turn_seconds"]

    async def test_treatment_spends_the_budget_it_does_have(self):
        """Not a fast-fail-at-any-cost: the point is to stop before the deadline,
        not to stop at the first failure. A ladder that gave up after one attempt
        whenever a deadline existed would pass every other test in this class and
        be a regression."""
        outcome = await _run_turn(**self.PARAMS, bind=True)
        assert outcome.attempts >= 2


@pytest.mark.integration
@pytest.mark.asyncio
class TestTheBackoffIsPartOfWhatIsBudgeted:
    """The backoff is spent, so it must be checked BEFORE it is slept.

    A ladder that only decided whether to *start an attempt* would still sleep a
    backoff it cannot afford and be cancelled inside it — the same failure with
    a longer fuse, and one that no amount of clamping the attempts can reach.
    Sized so the ladder's third backoff alone outlives what is left: with the
    check the turn answers at ~1.4s of its 2s, without it the turn is cancelled
    asleep.
    """

    PARAMS = dict(hang_seconds=0.2, turn_seconds=2.0, base_delay=1.0)

    async def test_control_is_cancelled_inside_a_backoff(self):
        outcome = await _run_turn(**self.PARAMS, bind=False)
        assert outcome.error_code == CANCELLED

    async def test_treatment_refuses_the_backoff_instead_of_sleeping_it(self):
        outcome = await _run_turn(**self.PARAMS, bind=True)
        assert outcome.error_code == TURN_BUDGET_EXHAUSTED
        assert outcome.elapsed < self.PARAMS["turn_seconds"]


@pytest.mark.integration
@pytest.mark.asyncio
class TestACoherentConfigurationIsUnaffected:
    """The inertness control.

    When the ladder fits, binding the deadline must change NOTHING — same number
    of attempts, same error code. Without this, a guard that simply truncated
    every ladder would look like a fix.
    """

    PARAMS = dict(hang_seconds=0.1, turn_seconds=3.0, base_delay=0.05)

    async def test_control_and_treatment_agree(self):
        control = await _run_turn(**self.PARAMS, bind=False)
        treatment = await _run_turn(**self.PARAMS, bind=True)

        assert control.error_code == "RETRY_EXHAUSTED"
        assert treatment.error_code == control.error_code
        assert treatment.attempts == control.attempts == 4

    async def test_a_fast_failing_provider_keeps_every_retry(self):
        """The retry budget is estimated from what attempts actually COST.

        A provider returning a 503 immediately costs nothing, so the estimate
        must not refuse retries on it — otherwise the fix would trade #1278's
        two-minute wait for a system that gives up on the first transient blip.
        """
        outcome = await _run_turn(
            hang_seconds=0.0, turn_seconds=1.0, base_delay=0.02, bind=True
        )
        assert outcome.error_code == "RETRY_EXHAUSTED"
        assert outcome.attempts == 4

    async def test_a_successful_call_is_returned_unchanged_under_a_deadline(self):
        """The happy path runs through ``asyncio.wait_for`` now, and must not
        notice. Every other test here drives a failure, so nothing else would
        catch a clamp that mangled or swallowed a real result."""
        sentinel = object()

        async def succeeding_provider():
            await asyncio.sleep(0.01)
            return sentinel

        handler = LLMErrorHandler(RetryConfig(base_delay_seconds=0.05))
        with bind_turn_deadline(3.0):
            result, error = await handler.with_retry(operation=succeeding_provider)

        assert result is sentinel
        assert error is None

    async def test_a_non_retryable_failure_keeps_its_own_classification(self):
        """The budget must not hijack a verdict the classifier already reached.

        A permanent billing failure is not retryable at any budget, and telling
        the operator the request budget ran out would send them to the timeout
        settings instead of to their provider account.
        """
        from faultmaven.exceptions import QUOTA_EXHAUSTED, LLMException

        async def out_of_credits():
            raise LLMException(
                "You exceeded your current quota, please check your plan and "
                "billing details",
                status_code=429,
            )

        handler = LLMErrorHandler(RetryConfig(base_delay_seconds=0.05))
        # A budget so small every retry would be refused, to prove the code
        # comes from the classifier and not from the budget check.
        with bind_turn_deadline(SCALED_RESERVE + 0.05):
            result, error = await handler.with_retry(operation=out_of_credits)

        assert result is None
        assert error is not None
        assert error.error_code == QUOTA_EXHAUSTED


@pytest.mark.integration
@pytest.mark.asyncio
class TestLaterLaddersInTheSameTurn:
    """#1278's actual complaint: one dead provider, many ladders, one budget.

    A turn makes several LLM calls (classification, investigation, the tool
    loop). Before the fix each one started a fresh ladder against the same dead
    provider until the turn cap fired. The budget is turn-wide, so a later ladder
    must see what the earlier ones spent.
    """

    async def test_control_the_turn_is_cancelled_re_laddering(self):
        outcome = await _run_turn(
            hang_seconds=0.3,
            turn_seconds=2.0,
            base_delay=0.05,
            bind=False,
            ladders=5,
        )
        assert outcome.error_code == CANCELLED

    async def test_treatment_the_turn_answers_before_its_deadline(self):
        outcome = await _run_turn(
            hang_seconds=0.3,
            turn_seconds=2.0,
            base_delay=0.05,
            bind=True,
            ladders=5,
        )
        assert outcome.error_code == TURN_BUDGET_EXHAUSTED
        assert outcome.elapsed < 2.0

    async def test_provider_calls_do_not_scale_with_the_number_of_ladders(self):
        """The anti-amplification property, stated as a limit rather than a count.

        A later ladder is a fresh ``LLMErrorHandler`` with no cost estimate of
        its own, so it does get ONE clamped attempt against whatever budget the
        earlier ladders left. That is bounded by construction — the clamp makes
        the attempt cost exactly the leftover, after which every further ladder
        finds no budget and calls nothing — so the count SATURATES. Asserting
        the saturation is the discriminating test: before the fix the count grew
        with the ladders until the turn cap fired.

        Measured against a hung provider (0.5s hang, 1.5s turn): 2 ladders and 50
        ladders both cost 3 provider calls and 1.20s.
        """
        params = dict(hang_seconds=0.5, turn_seconds=1.5, base_delay=0.05, bind=True)
        few = await _run_turn(**params, ladders=2)
        many = await _run_turn(**params, ladders=50)

        assert many.attempts == few.attempts
        assert many.error_code == few.error_code == TURN_BUDGET_EXHAUSTED
        assert many.elapsed < params["turn_seconds"]
        # ... and 25x the ladders costs no more wall clock than 2x.
        assert many.elapsed < few.elapsed + 0.3


@pytest.mark.integration
@pytest.mark.asyncio
class TestEveryExitCarriesAClassifiedCode:
    """Every early exit must name itself, or it is worse than the 504 it replaced.

    ``_first_engine_error_code`` reads ``ErrorResult.error_code``, and a code the
    API boundary does not recognise falls through to a bare
    ``500 SERVICE_ERROR``. The ladder has TWO early exits — the retry gate, and
    the "no budget even to start" check at the top of the loop — and the second
    is reachable only after a retry has already been granted, which is the one
    place a half-finished ``ErrorResult`` is lying around to be returned by
    mistake.
    """

    async def test_the_gate_exit_is_classified(self):
        outcome = await _run_turn(
            hang_seconds=5.0, turn_seconds=2.0, base_delay=0.05, bind=True
        )
        assert outcome.error_code == TURN_BUDGET_EXHAUSTED

    async def test_the_no_budget_exit_is_classified(self, monkeypatch):
        """Driven through a scripted budget rather than the clock.

        The branch needs the budget to be affordable at the retry decision and
        gone by the next iteration — a window a wall-clock test can only hit by
        luck. Scripting it is what makes the assertion about the code rather
        than about the scheduler.
        """
        from faultmaven.core.investigation import llm_error_handler as module

        budgets = iter([10.0, 10.0, 0.0])
        monkeypatch.setattr(
            module, "spendable_turn_budget", lambda *a, **k: next(budgets, 0.0)
        )

        calls = {"n": 0}

        async def failing_provider():
            calls["n"] += 1
            raise ExternalCallTimeout(
                "External call to LLM_Providers.generate timed out after 1s",
                service="LLM_Providers",
                operation="generate",
                timeout=1,
            )

        # base_delay 0 so the granted retry costs no wall clock; the budget
        # sequence, not the sleep, is what moves the ladder along.
        handler = LLMErrorHandler(RetryConfig(base_delay_seconds=0.0))
        result, error = await handler.with_retry(operation=failing_provider)

        assert result is None
        assert calls["n"] == 1, "the scripted budget should grant exactly one attempt"
        assert error is not None
        assert error.error_code == TURN_BUDGET_EXHAUSTED
        # The provider's own wording survives for diagnostics.
        assert isinstance(error.original_exception, ExternalCallTimeout)
