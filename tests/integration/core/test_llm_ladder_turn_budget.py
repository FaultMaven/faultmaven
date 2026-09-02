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
from faultmaven.core.investigation.turn_budget import (
    bind_turn_deadline,
    clamp_to_turn_budget,
)
from faultmaven.exceptions import TURN_BUDGET_EXHAUSTED, ExternalCallTimeout
from faultmaven.infrastructure.base_client import BaseExternalClient

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


class _HangingProvider(BaseExternalClient):
    """A real ``BaseExternalClient`` with the router's own breaker settings.

    Real, not a double, because the whole question is which of that class's
    exception handlers runs — and a double would answer it by construction.
    """

    def __init__(self, hang_seconds):
        super().__init__(
            client_name="test_provider",
            service_name="LLM_Providers",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,
            circuit_breaker_timeout=30,
        )
        self.hang_seconds = hang_seconds

    async def health_check(self):
        return {"status": "ok"}

    async def _hang(self):
        await asyncio.sleep(self.hang_seconds)
        return "never reached"

    async def generate(self, configured_timeout, clamp):
        """``clamp=True`` is what ``LLMRouter._resolve_timeout`` now does."""
        timeout = (
            clamp_to_turn_budget(configured_timeout) if clamp else configured_timeout
        )
        return await self.call_external(
            "route_llm_request", self._hang, timeout=timeout, retries=0
        )


async def _turn_against_provider(*, hang, turn_seconds, base_delay, clamp):
    provider = _HangingProvider(hang)
    attempts = {"n": 0}

    async def op():
        attempts["n"] += 1
        return await provider.generate(hang, clamp)

    handler = LLMErrorHandler(RetryConfig(base_delay_seconds=base_delay))
    with bind_turn_deadline(turn_seconds):
        try:
            _, error = await asyncio.wait_for(
                handler.with_retry(operation=op), timeout=turn_seconds
            )
            code = error.error_code if error is not None else None
        except asyncio.TimeoutError:
            code = CANCELLED
    return provider.circuit_breaker, attempts["n"], code


@pytest.mark.integration
@pytest.mark.asyncio
class TestTheCutShortCallStillReachesTheBreaker:
    """The other half of #1278: fast-failing the SECOND outage turn.

    Bounding the turn makes each failure honest, but a provider outage should
    also stop costing a full budget after the breaker has seen enough of it.
    That only happens if a cut-short attempt is RECORDED, and which timeout
    fires decides whether it is: the call's own timeout raises
    ``ExternalCallTimeout`` from ``call_external``'s ``except
    asyncio.TimeoutError`` and records a failure, while an outer ``wait_for``
    cancels the coroutine and ``CancelledError`` reaches none of that method's
    handlers.

    Measured on the shape where the two differ — one attempt costing more than
    the whole turn, i.e. the ``T=180 / A=120`` row of the configuration table.
    """

    PARAMS = dict(hang=5.0, turn_seconds=2.0, base_delay=0.05)

    async def test_control_an_outer_clamp_records_nothing(self):
        """Pre-fix behaviour, asserted rather than described."""
        breaker, attempts, code = await _turn_against_provider(
            **self.PARAMS, clamp=False
        )
        assert attempts == 1
        assert code == TURN_BUDGET_EXHAUSTED  # the turn is honest ...
        assert breaker.failure_count == 0  # ... and the breaker learns nothing
        assert breaker.state == "closed"

    async def test_treatment_clamping_the_calls_own_timeout_records_it(self):
        breaker, attempts, code = await _turn_against_provider(
            **self.PARAMS, clamp=True
        )
        assert attempts == 1
        assert code == TURN_BUDGET_EXHAUSTED
        assert breaker.failure_count == 1, (
            "a cut-short attempt must still count against the breaker, or a "
            "provider outage costs a full turn budget forever"
        )

    async def test_the_breaker_opens_across_turns_and_then_fast_fails(self):
        """The end #1278 asks for: the outage stops costing a full budget.

        Three turns' worth of failures on one provider, then the breaker is
        open and the next call never reaches it.
        """
        provider = _HangingProvider(5.0)
        for _ in range(3):
            with bind_turn_deadline(2.0):
                with pytest.raises(Exception):
                    await provider.generate(5.0, clamp=True)
        assert provider.circuit_breaker.failure_count >= 3
        assert provider.circuit_breaker.state == "open"

        started = time.monotonic()
        with pytest.raises(Exception):
            await provider.generate(5.0, clamp=True)
        assert time.monotonic() - started < 0.5, "an open breaker must fast-fail"


@pytest.mark.integration
@pytest.mark.asyncio
class TestWhichVerdictTheBudgetReports:
    """``TURN_BUDGET_EXHAUSTED`` must mean the budget COST the caller attempts.

    Its documented remediation is ``GET /admin/config/status``. Reporting it
    when the ladder in fact made every attempt that could reach a provider
    sends the operator to a page that says the timeouts are coherent — a
    diagnostic dead end. The verdict therefore turns on whether a
    provider-reaching attempt was actually lost, which is a structural question
    about the ladder's own length and not a question about the circuit breaker,
    whose state this layer cannot observe.
    """

    async def test_full_length_ladder_reports_a_provider_verdict(self):
        """3 of 3 paid attempts made; only the breaker-refused iteration was
        skipped, and skipping it cannot have changed the answer."""
        outcome = await _run_turn(
            hang_seconds=0.1, turn_seconds=1.3, base_delay=0.1, bind=True
        )
        assert outcome.attempts == 3
        assert outcome.error_code == "RETRY_EXHAUSTED"

    async def test_a_lost_attempt_reports_a_configuration_verdict(self):
        outcome = await _run_turn(
            hang_seconds=0.5, turn_seconds=1.3, base_delay=0.1, bind=True
        )
        assert outcome.attempts < 3
        assert outcome.error_code == TURN_BUDGET_EXHAUSTED

    async def test_the_two_verdicts_discriminate_on_the_same_turn_budget(self):
        """Same A, same backoffs — only the attempt cost differs, and the code
        flips exactly where a paid attempt starts being lost. A single shape
        would not show that the code tracks anything."""
        full = await _run_turn(
            hang_seconds=0.1, turn_seconds=1.3, base_delay=0.1, bind=True
        )
        lost = await _run_turn(
            hang_seconds=0.5, turn_seconds=1.3, base_delay=0.1, bind=True
        )
        assert full.error_code != lost.error_code
        assert full.attempts > lost.attempts


@pytest.mark.integration
@pytest.mark.asyncio
class TestATruncationRetryKeepsItsRecoveryCode:
    """A budget refusal must not disable the minimal-prompt degrade (#662).

    The engine selects that recovery on ``TOKEN_LIMIT``. If the budget refuses
    the truncation retry and the ladder reports a budget/availability code
    instead, a recoverable overflow becomes a hard failure whose message blames
    the provider.
    """

    async def test_the_budget_refusal_still_reports_token_limit(self, monkeypatch):
        from faultmaven.core.investigation import llm_error_handler as module
        from faultmaven.exceptions import TOKEN_LIMIT

        monkeypatch.setattr(module, "spendable_turn_budget", lambda *a, **k: 0.0)
        handler = LLMErrorHandler(RetryConfig(base_delay_seconds=0.0))

        result = await handler._retry_or_exhaust(
            0, next_attempt_seconds=5.0, no_budget_code=TOKEN_LIMIT
        )

        assert result.error_code == TOKEN_LIMIT

    async def test_without_the_override_it_reports_the_budget(self, monkeypatch):
        """The control: the same refusal on the ordinary retryable branch."""
        from faultmaven.core.investigation import llm_error_handler as module

        monkeypatch.setattr(module, "spendable_turn_budget", lambda *a, **k: 0.0)
        handler = LLMErrorHandler(RetryConfig(base_delay_seconds=0.0))

        result = await handler._retry_or_exhaust(0, next_attempt_seconds=5.0)

        assert result.error_code == TURN_BUDGET_EXHAUSTED


@pytest.mark.integration
class TestTheRouterAppliesTheClamp:
    """The seam that makes the budget cover EVERY LLM call in a turn.

    ``LLMRouter._resolve_timeout`` is the sole source of the ``timeout`` handed
    to ``call_external`` for a router-borne call, so clamping there bounds the
    investigation ladder, intent classification (``intent_resolver``) and
    KB/document answer synthesis (``document_qa_tool``) alike. Without it only
    the one ``with_retry`` call site was budgeted, and a hang anywhere else in
    the turn still produced the opaque 504 this work removes.

    Asserted on the real router rather than on ``clamp_to_turn_budget``, which
    is already unit-tested: the question here is whether the router calls it.
    """

    @pytest.fixture(scope="class")
    def router(self):
        from faultmaven.infrastructure.llm.router import LLMRouter

        return LLMRouter()

    def test_outside_a_turn_the_configured_ceiling_is_untouched(self, router):
        assert router._resolve_timeout() == pytest.approx(router.request_timeout)

    def test_inside_a_turn_the_ceiling_is_cut_to_the_remaining_budget(self, router):
        deadline = max(1.0, router.request_timeout / 10.0)
        with bind_turn_deadline(deadline):
            clamped = router._resolve_timeout()

        assert clamped < router.request_timeout
        assert clamped == pytest.approx(
            deadline - turn_budget.TURN_BUDGET_RESERVE_SECONDS, abs=0.1
        )

    def test_the_router_applies_the_SHARED_clamp_not_a_local_min(self, router):
        """Pins the seam between the two halves of the breaker fix.

        One test proves the router narrows its ceiling; another proves a call
        cut short by ``clamp_to_turn_budget`` still records a breaker failure.
        Neither is worth much unless the router narrows it with THAT function —
        a hand-rolled ``min`` here would satisfy the first test and silently
        drop the second's guarantee.
        """
        unbound = router._resolve_timeout()
        with bind_turn_deadline(max(1.0, unbound / 10.0)):
            assert router._resolve_timeout() == pytest.approx(
                clamp_to_turn_budget(unbound), abs=0.05
            )

    def test_a_turn_longer_than_the_ceiling_leaves_it_alone(self, router):
        """The clamp is a ceiling, never a floor: a generous turn must not
        lengthen a deliberately short provider timeout."""
        with bind_turn_deadline(router.request_timeout * 10):
            assert router._resolve_timeout() == pytest.approx(router.request_timeout)
