"""Each attempt of a routed walk runs on its own provider's deadline (#2983).

The router used to fix one timeout for the whole walk, the CHAT provider's,
before the registry chose a provider: a fallback, or a role pinned to another
provider, ran on the primary's deadline. Now each attempt is bounded by its own
provider's ``T`` (``provider_timeout``), clamped to the turn's spendable budget;
a timeout at its own ceiling is recorded against that provider and the walk
moves on; the router's outer bound is later than every attempt's.

The rig is #2319's: a real ``LLMRouter``, a real ``ProviderRegistry`` and the
real dispatch over labelled stub adapters, on a virtual clock (this directory's
``conftest.py``), so every elapsed figure is the schedule the code chose.

Each pin names the mutant it fails on.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from faultmaven.config import retry_budget
from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.turn_budget import bind_turn_deadline
from faultmaven.exceptions import (
    TURN_BUDGET_EXHAUSTED,
    LLMAttemptTimeout,
    LLMTurnBudgetSpent,
)
from faultmaven.infrastructure.llm import router as router_module
from faultmaven.infrastructure.llm.dispatch import route_model_call
from faultmaven.infrastructure.llm.providers import base as providers_base
from faultmaven.infrastructure.llm.providers import registry as registry_module
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.llm.providers.registry import ProviderRegistry
from tests.integration.core.virtual_time.test_tool_loop_deadline_handoff_2319 import (  # noqa: F401 - autouse fixtures
    _no_metering,
    _no_network,
    _virtual_clocks,
    adapter,
    answers_503,
    build_router,
    sends,
)
from tests.model_content import vetted
from tests.wallclock import virtual_now

pytestmark = [pytest.mark.integration, pytest.mark.llm]

BASE = 30.0


def answers_after(stub, seconds: float, before=None) -> None:
    """``stub`` answers every send after ``seconds``, calling ``before()``
    first when given (its value is kept in ``stub.seen``)."""

    async def _generate(**kwargs):
        stub.sends.append(("single", virtual_now()))
        if before is not None:
            stub.seen = before()
            await asyncio.sleep(stub.seen or 0.0)
        await asyncio.sleep(seconds)
        return LLMResponse(
            content="ok",
            confidence=0.95,
            provider=stub.LABEL,
            model=f"{stub.LABEL}-default",
            tokens_used=1,
            response_time_ms=1,
        )

    stub._generate = _generate


def rig(monkeypatch, chain, overrides):
    router = build_router(monkeypatch, chain, request_timeout=BASE)
    monkeypatch.setattr(router.settings.llm, "provider_timeout_overrides", overrides)
    monkeypatch.setattr(
        registry_module.ProviderRegistry,
        "_get_routing_order",
        lambda self: list(self._fallback_chain),
    )
    return router


def health(router, label):
    return router.registry._provider_states[label].consecutive_failures


async def timed(coro):
    started = virtual_now()
    try:
        return await coro, None, virtual_now() - started
    except Exception as error:  # noqa: BLE001 - the outcome under test
        return None, error, virtual_now() - started


@pytest.mark.asyncio
class TestEachAttemptRunsOnItsOwnProvidersDeadline:
    async def test_the_fallback_runs_to_its_own_deadline(self, monkeypatch):
        """The issue's: the primary at the 30 s base fails at once; the
        fallback, overridden to 300 s, answers at 120 s. On ``main`` the walk
        is cut at 30 s. Mutant: the walk's bound from CHAT's ``T``."""
        primary = adapter("gemini", single=[answers_503])
        fallback = adapter("fireworks")
        answers_after(fallback, 120.0)
        router = rig(monkeypatch, [primary, fallback], {"fireworks": 300})

        response, error, elapsed = await timed(
            route_model_call(router, prompt=vetted("p"))
        )

        assert error is None and response.content == "ok"
        assert elapsed == pytest.approx(120.0)
        assert health(router, "gemini") == 1 and health(router, "fireworks") == 0

    async def test_a_pinned_role_runs_on_its_providers_override(self, monkeypatch):
        """``provider_override`` to a provider with its own override runs on
        it; on ``main`` on CHAT's 30 s. Mutant: as above."""
        chat = adapter("gemini")
        pinned = adapter("fireworks")
        answers_after(pinned, 120.0)
        router = rig(monkeypatch, [chat, pinned], {"fireworks": 300})

        response, error, elapsed = await timed(
            route_model_call(router, prompt=vetted("p"), provider_override="fireworks")
        )

        assert error is None and response.content == "ok"
        assert elapsed == pytest.approx(120.0)
        assert sends(chat) == []

    async def test_a_hung_primary_is_recorded_on_its_own_health(self, monkeypatch):
        """The primary hangs to its own 30 s, is recorded on its health (not
        the fallback's), and the fallback answers. Mutant: the attempt's
        timeout not raised inside the walk (the outer bound cuts it)."""
        primary = adapter("gemini")
        fallback = adapter("fireworks")
        answers_after(fallback, 10.0)
        router = rig(monkeypatch, [primary, fallback], {"fireworks": 45})

        response, error, elapsed = await timed(
            route_model_call(router, prompt=vetted("p"))
        )

        assert error is None and response.content == "ok"
        assert elapsed == pytest.approx(40.0)
        assert sends(fallback) == [pytest.approx(30.0)]
        assert health(router, "gemini") == 1 and health(router, "fireworks") == 0
        assert router.circuit_breaker.failure_count == 0

    async def test_a_chain_that_times_out_everywhere_is_a_breaker_failure(
        self, monkeypatch
    ):
        """Every attempt hangs to its own ceiling (30 s, then 45 s): each is
        recorded on its own health, and ``call_external`` records the chain's
        failure on the breaker, the timeout naming the provider and its ``t``.
        Mutant: ``call_external`` re-wraps it (the message names its bound)."""
        router = rig(
            monkeypatch,
            [adapter("gemini"), adapter("fireworks")],
            {"fireworks": 45},
        )

        _, error, elapsed = await timed(route_model_call(router, prompt=vetted("p")))

        assert isinstance(error, LLMAttemptTimeout)
        assert str(error) == "LLM provider 'fireworks' timed out after 45s"
        assert error.provider == "fireworks" and error.timeout == 45.0
        assert elapsed == pytest.approx(75.0)
        assert health(router, "gemini") == 1 and health(router, "fireworks") == 1
        assert router.circuit_breaker.failure_count == 1

    async def test_an_override_below_the_base_lowers_the_attempt(self, monkeypatch):
        """An override is the provider's ``T`` outright: 10 s under a 30 s
        base. Mutant: ``max(override, base)``."""
        router = rig(monkeypatch, [adapter("groq")], {"groq": 10})

        _, error, elapsed = await timed(route_model_call(router, prompt=vetted("p")))

        assert isinstance(error, LLMAttemptTimeout)
        assert elapsed == pytest.approx(10.0)


@pytest.mark.asyncio
class TestTheTurnBudget:
    async def test_a_turn_clamped_cut_is_not_the_providers_failure(self, monkeypatch):
        """A 20 s turn spends 15.5 s (less the 1.0 s step and 3.5 s commit
        reserves): the primary is cut there, short of its 30 s, and its health
        records nothing; the walk then stops, so the provider it never tried
        records nothing either. Mutants: health records any timeout; the walk
        tries the next provider with a spent budget."""
        primary, fallback = adapter("gemini"), adapter("fireworks")
        router = rig(monkeypatch, [primary, fallback], {})

        with bind_turn_deadline(20.0):
            _, error, elapsed = await timed(
                route_model_call(router, prompt=vetted("p"))
            )

        assert isinstance(error, LLMAttemptTimeout) and error.provider == "gemini"
        assert elapsed == pytest.approx(15.5)
        assert sends(fallback) == []
        assert health(router, "gemini") == 0 and health(router, "fireworks") == 0

    @pytest.mark.parametrize("left", [4.0, 3.0], ids=["under-4.5s", "under-3.75s"])
    @pytest.mark.parametrize("path", ["router", "walk"])
    async def test_a_spent_turn_calls_no_provider_and_records_nothing(
        self, monkeypatch, left, path
    ):
        """Under the step and commit reserves (4.5 s), and under the backstop's
        (3.75 s): the call is refused before any provider, and neither the
        breaker nor any provider's health records it. ``walk`` lifts the
        router's own check, so the walk's stop is what refuses it, through
        ``call_external``; the router refuses before entering it. Mutants: the
        walk's stop raises a timeout (recorded); no router refusal."""
        primary, fallback = adapter("gemini"), adapter("fireworks")
        router = rig(monkeypatch, [primary, fallback], {})
        if path == "walk":
            monkeypatch.setattr(router_module, "spendable_turn_budget", lambda: None)

        with bind_turn_deadline(left):
            _, error, elapsed = await timed(
                route_model_call(router, prompt=vetted("p"))
            )

        assert isinstance(error, LLMTurnBudgetSpent)
        assert not isinstance(error, TimeoutError)
        assert error.error_code == TURN_BUDGET_EXHAUSTED
        assert elapsed == pytest.approx(0.0)
        assert sends(primary) == sends(fallback) == []
        assert health(router, "gemini") == 0 and health(router, "fireworks") == 0
        assert router.circuit_breaker.failure_count == 0
        entered = router.connection_metrics["total_calls"]
        assert entered == (1 if path == "walk" else 0)

    async def test_the_walks_stop_is_logged_as_a_spent_turn(self, monkeypatch, caplog):
        """No provider was tried, so the walk's stop is no chain failure: a
        warning, never "All providers failed" at error with a traceback.
        Mutant: the stop falls through to the chain-failure report."""
        router = rig(monkeypatch, [adapter("gemini"), adapter("fireworks")], {})
        monkeypatch.setattr(router_module, "spendable_turn_budget", lambda: None)

        with caplog.at_level(logging.WARNING, logger=registry_module.__name__):
            with bind_turn_deadline(4.0):
                _, error, _ = await timed(route_model_call(router, prompt=vetted("p")))

        assert isinstance(error, LLMTurnBudgetSpent)
        walk = [r for r in caplog.records if r.name == registry_module.__name__]
        assert [r.levelno for r in walk] == [logging.WARNING]
        assert "Turn budget spent" in walk[0].getMessage()
        assert walk[0].exc_info is None

    async def test_its_outcome_is_the_turn_budgets(self):
        """In the ladder and at the HTTP boundary: ``TURN_BUDGET_EXHAUSTED``,
        503 with ``Retry-After``, never a deadline-shaped retry."""
        from faultmaven.api.exception_handlers import llm_service_error_http_exception
        from faultmaven.core.investigation.llm_error_handler import (
            ErrorAction,
            is_deadline_shaped,
        )

        result = await LLMErrorHandler(RetryConfig()).handle_error(LLMTurnBudgetSpent())
        assert result.action is ErrorAction.FAIL
        assert result.error_code == TURN_BUDGET_EXHAUSTED
        assert not is_deadline_shaped(LLMTurnBudgetSpent())
        http = llm_service_error_http_exception(LLMTurnBudgetSpent())
        assert http.status_code == 503 and http.headers["Retry-After"] == "30"

    @pytest.mark.parametrize("turn", [None, 120.0], ids=["outside", "inside"])
    async def test_a_providers_own_timeout_names_no_bound(self, monkeypatch, turn):
        """A provider raising a ``TimeoutError`` of its own: ``call_external``
        names no figure it did not set (``None`` outside a turn, the backstop
        inside one). Mutant: the wrap always names ``timeout``."""

        async def times_out_itself(**kwargs):
            raise TimeoutError("socket read timed out")

        stub = adapter("gemini")
        stub._generate = times_out_itself
        router = rig(monkeypatch, [stub], {})

        with bind_turn_deadline(turn):
            _, error, _ = await timed(route_model_call(router, prompt=vetted("p")))

        assert str(error) == (
            "External call to LLM_Providers.route_llm_request timed out inside "
            "the call"
        )
        assert error.timeout is None
        assert type(error.__cause__) is TimeoutError

    async def test_an_attempt_reads_the_registrys_own_settings(self, monkeypatch):
        """The attempt's ``T`` comes from the settings the registry built its
        clients with, not the live ones. Mutant: ``provider_timeout(name)``."""
        router = rig(monkeypatch, [adapter("groq")], {"groq": 50})
        router.registry.settings = SimpleNamespace(
            llm=SimpleNamespace(
                provider_timeout_overrides={"groq": 7}, request_timeout=int(BASE)
            )
        )

        _, error, elapsed = await timed(route_model_call(router, prompt=vetted("p")))

        assert isinstance(error, LLMAttemptTimeout)
        assert elapsed == pytest.approx(7.0)

    @pytest.mark.parametrize(
        "own,waited,elapsed", [(45, 12.0, 22.0), (20, None, 10.0)], ids=["45s", "20s"]
    )
    async def test_a_429_wait_spends_the_attempts_deadline(
        self, monkeypatch, own, waited, elapsed
    ):
        """The primary fails after 10 s; the fallback is asked to wait 12 s,
        taken only when twice it is less than what is left of the attempt's
        own deadline: at 45 s it waits and answers at 22 s (on ``main`` the
        wait read the walk's 20 s left and was refused), at 20 s it is refused.
        Mutant (the 20 s case): ``_call_deadline`` not rebound for the
        attempt, so outside a turn nothing bounds the wait."""

        async def fails_after_10(**kwargs):
            primary.sends.append(("single", virtual_now()))
            await asyncio.sleep(10.0)
            raise answers_503()

        primary, fallback = adapter("gemini"), adapter("fireworks")
        primary._generate = fails_after_10
        answers_after(
            fallback,
            0.0,
            before=lambda: providers_base.rate_limit_wait({"retry-after": "12"}, 0),
        )
        router = rig(monkeypatch, [primary, fallback], {"fireworks": own})

        response, error, took = await timed(
            route_model_call(router, prompt=vetted("p"))
        )

        assert error is None and response.content == "ok"
        assert fallback.seen == waited
        assert took == pytest.approx(elapsed)


@pytest.mark.asyncio
async def test_a_non_strict_chain_that_hangs_is_refused_its_retry(monkeypatch):
    """Two hung providers at the 30 s base in a 120 s turn: the first ladder
    attempt walks both (60 s), and its backoff plus another 60 s does not fit
    what is left, so the ladder stops there. On ``main`` the walk was cut at
    CHAT's 30 s, the second provider never tried, and the retry granted (62 s).
    Mutant: the outer bound at CHAT's ``T``."""
    router = rig(monkeypatch, [adapter("gemini"), adapter("fireworks")], {})

    async def attempt():
        return await route_model_call(router, prompt=vetted("p"))

    started = virtual_now()
    with bind_turn_deadline(120.0):
        _, error = await asyncio.wait_for(
            LLMErrorHandler(RetryConfig()).with_retry(operation=attempt), 120.0
        )

    assert error.error_code == TURN_BUDGET_EXHAUSTED
    assert virtual_now() - started == pytest.approx(60.0)


# --- The ladder report -------------------------------------------------------


def _settings(provider, *, strict, overrides=None, pinned=None):
    """Settings that configure gemini and fireworks (their keys), no other."""
    return SimpleNamespace(
        llm=SimpleNamespace(
            provider=provider,
            request_timeout=int(BASE),
            provider_timeout_overrides=overrides or {},
            strict_provider_mode=strict,
            structured_output_provider=pinned,
            gemini_api_key="k",
            fireworks_api_key="k",
        ),
        agent=SimpleNamespace(timeout_for_provider=lambda _name: 120),
    )


@pytest.fixture
def initialised(monkeypatch):
    """No registry exists: the report must not build one."""
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)
    monkeypatch.setattr(registry_module, "_registry", None)
    yield
    assert registry_module._registry is None, "the report built the registry"


class TestTheLadderReportPricesTheRoutedWalk:
    def test_a_single_strict_provider_is_unchanged(self, initialised):
        """``2T + 2`` and ``3T + 14`` at T = 30 s: both fit a 120 s turn."""
        budget = retry_budget.describe_retry_ladder_budget(
            _settings("gemini", strict=True)
        )
        assert budget.deadline_shaped.full_ladder_seconds == 62.0
        assert budget.status_shaped.full_ladder_seconds == 104.0
        assert budget.fits

    def test_a_non_strict_chain_is_priced_at_its_sum(self, initialised):
        """Both configured providers hang: 60 s an attempt, ``2·60 + 2`` does
        not fit, and no registry is built for it. Mutants: CHAT's ``T`` alone;
        the chain read from ``get_registry()``."""
        budget = retry_budget.describe_retry_ladder_budget(
            _settings("gemini", strict=False)
        )
        assert budget.deadline_shaped.full_ladder_seconds == 122.0
        assert not budget.fits

    def test_a_pinned_structured_output_provider_is_priced(self, initialised):
        """A schema-bound role pinned to a slower provider costs its ``T``."""
        settings = _settings(
            "gemini",
            strict=True,
            overrides={"fireworks": 50},
            pinned=SimpleNamespace(value="fireworks"),
        )
        assert retry_budget.routed_attempt_seconds(settings) == 50.0

    def test_the_default_local_ladder_still_fits(self, initialised):
        """No schema timeout for ``local`` any more: it runs at the base, and
        its strict ladder (62 s) fits a 120 s turn."""
        budget = retry_budget.describe_retry_ladder_budget(
            _settings("local", strict=True)
        )
        assert budget.deadline_shaped.full_ladder_seconds == 62.0
        assert budget.fits


def test_an_operator_override_for_local_reaches_its_client():
    """And an override below the base lowers the client as it lowers the
    attempt (``test_an_override_below_the_base_lowers_the_attempt``)."""
    llm = SimpleNamespace(
        local_model="qwen3-32b",
        local_url="http://localhost:8000/v1",
        local_tool_calling=None,
        max_retries=1,
        request_timeout=int(BASE),
        provider_timeout_overrides={"local": 10},
    )
    registry = ProviderRegistry(settings=SimpleNamespace(llm=llm))
    config = registry._create_provider_config(
        "local", registry_module.PROVIDER_SCHEMA["local"]
    )
    assert config.timeout == 10.0
