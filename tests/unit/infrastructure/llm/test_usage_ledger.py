"""The LLM usage ledger's writer, in isolation (#640).

What these pin, against the in-memory ledger and a recording failure counter:

* the turn tracker buckets every billed call by ``(provider, model, outcome)``,
  with the price table's cost, unpriced calls counted, a self-hosted call priced
  at $0 rather than unpriced, and discarded low-confidence calls kept apart;
* ``record_provider_call``'s three branches — a live turn accrues and schedules
  nothing; a flushed turn and no turn each schedule exactly one own-row write,
  the first carrying the TURN's actor, the second the request's;
* the fail-open contract: every call that does not reach a row is counted under
  its reason (``no_tenant``, ``not_composed``, ``store_error``, ``no_loop``), a
  store error is logged without the row, and no caller ever sees an exception;
* the flush and the shutdown drain.

The paths that run these — a real turn, a real router, a real database — are
``tests/integration/test_llm_usage_ledger_coverage.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    get_current_actor_user_id,
    set_current_actor_user_id,
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.infrastructure.llm import usage_ledger
from faultmaven.infrastructure.llm.metering import (
    TurnTokenTracker,
    active_token_tracker,
    record_provider_call,
)
from faultmaven.infrastructure.llm.pricing import estimate_cost_usd
from faultmaven.infrastructure.llm.usage_ledger import (
    REASON_ATTRIBUTION_ERROR,
    REASON_NO_LOOP,
    REASON_NO_TENANT,
    REASON_NOT_COMPOSED,
    REASON_STORE_ERROR,
    SUBJECT_NONE,
    InMemoryUsageLedger,
    UsageAttribution,
    capture_attribution,
    drain_pending_usage_writes,
    flush_turn,
    install_usage_ledger,
    schedule_call_write,
)

pytestmark = pytest.mark.unit


@dataclass
class _Resp:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tokens_used: int = 0
    prompt_cache_hit: bool = False
    model: str = "claude-sonnet-4-6"


class _RecordingCounter:
    """Stands in for ``llm_usage_unpersisted_calls_total``: {reason: total}."""

    def __init__(self) -> None:
        self.counts: dict[str, float] = {}
        self._reason = None

    def labels(self, *, reason):
        self._reason = reason
        return self

    def inc(self, amount=1):
        self.counts[self._reason] = self.counts.get(self._reason, 0) + amount


@pytest.fixture
def unpersisted(monkeypatch) -> _RecordingCounter:
    counter = _RecordingCounter()
    monkeypatch.setattr(usage_ledger, "llm_usage_unpersisted_calls", counter)
    return counter


@pytest.fixture
def ledger():
    ledger = InMemoryUsageLedger()
    install_usage_ledger(ledger)
    yield ledger
    install_usage_ledger(None)


@pytest.fixture(autouse=True)
def clean_context():
    """Every test starts standalone, with no actor and no billing organization."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    set_current_actor_user_id(None)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    set_current_actor_user_id(None)
    install_usage_ledger(None)


@pytest.fixture
def multi_tenant(monkeypatch):
    from faultmaven.providers.tenancy import factory

    monkeypatch.setattr(
        factory, "requested_tenant_provider", lambda: factory.BUILTIN_MULTI
    )


def _in_turn(tracker, fn):
    token = active_token_tracker.set(tracker)
    try:
        return fn()
    finally:
        active_token_tracker.reset(token)


# =============================================================================
# The buckets
# =============================================================================


class TestTheBuckets:
    def _metered(self) -> TurnTokenTracker:
        tracker = TurnTokenTracker()

        def calls():
            record_provider_call(
                "anthropic", "claude-sonnet-4-6", _Resp(1000, 200, 5000, 300), 1.0
            )
            record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(500, 100), 1.0)
            record_provider_call(
                "anthropic",
                "claude-sonnet-4-6",
                _Resp(700, 50),
                1.0,
                outcome="low_confidence",
            )
            record_provider_call("openrouter", "mystery-model", _Resp(900, 90), 1.0)
            record_provider_call("local", "llama3.2", _Resp(4000, 400), 1.0)

        _in_turn(tracker, calls)
        return tracker

    def test_one_bucket_per_provider_model_outcome(self):
        tracker = self._metered()
        assert set(tracker.buckets) == {
            ("anthropic", "claude-sonnet-4-6", "kept"),
            ("anthropic", "claude-sonnet-4-6", "low_confidence"),
            ("openrouter", "mystery-model", "kept"),
            ("local", "llama3.2", "kept"),
        }
        kept = tracker.buckets[("anthropic", "claude-sonnet-4-6", "kept")]
        assert (kept.calls, kept.input_tokens, kept.output_tokens) == (2, 1500, 300)
        assert (kept.cache_read_tokens, kept.cache_write_tokens) == (5000, 300)
        # The buckets partition the turn: they sum to its totals.
        assert sum(b.calls for b in tracker.buckets.values()) == tracker.total_calls
        assert (
            sum(b.input_tokens for b in tracker.buckets.values())
            == tracker.input_tokens
        )
        assert sum(
            b.estimated_cost_usd for b in tracker.buckets.values()
        ) == pytest.approx(tracker.cost_usd)

    def test_a_buckets_cost_is_the_price_tables(self):
        tracker = self._metered()
        kept = tracker.buckets[("anthropic", "claude-sonnet-4-6", "kept")]
        first, _ = estimate_cost_usd(
            "anthropic",
            "claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=5000,
            cache_write_tokens=300,
        )
        second, _ = estimate_cost_usd(
            "anthropic", "claude-sonnet-4-6", input_tokens=500, output_tokens=100
        )
        assert kept.estimated_cost_usd == pytest.approx(first + second)
        assert kept.unpriced_calls == 0

    def test_an_unpriced_model_is_counted_not_priced_at_zero(self):
        tracker = self._metered()
        unknown = tracker.buckets[("openrouter", "mystery-model", "kept")]
        assert unknown.unpriced_calls == 1
        assert unknown.estimated_cost_usd == 0.0
        assert tracker.unpriced_calls == 1

    def test_a_self_hosted_call_is_priced_at_zero_not_unpriced(self):
        tracker = self._metered()
        local = tracker.buckets[("local", "llama3.2", "kept")]
        assert local.calls == 1
        assert local.unpriced_calls == 0
        assert local.estimated_cost_usd == 0.0

    def test_low_confidence_waste_is_its_own_count(self):
        tracker = self._metered()
        waste = tracker.buckets[("anthropic", "claude-sonnet-4-6", "low_confidence")]
        assert waste.calls == 1
        assert tracker.low_confidence_calls == 1
        # Still billed, so still in the turn's spend.
        assert waste.estimated_cost_usd > 0


# =============================================================================
# record_provider_call's three branches
# =============================================================================


class TestTheThreeBranches:
    async def test_a_live_turn_accrues_and_schedules_nothing(self, ledger):
        tracker = TurnTokenTracker(actor_user_id="u-turn")
        _in_turn(
            tracker,
            lambda: record_provider_call(
                "anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0
            ),
        )
        await drain_pending_usage_writes()
        assert tracker.total_calls == 1
        assert ledger.call_writes == 0
        assert not usage_ledger._pending_writes

    async def test_a_flushed_turn_schedules_one_row_carrying_the_turns_actor(
        self, ledger
    ):
        # The request's actor differs, to prove which one is used.
        set_current_actor_user_id("u-request")
        tracker = TurnTokenTracker(actor_user_id="u-turn", flushed=True)
        _in_turn(
            tracker,
            lambda: record_provider_call(
                "anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0
            ),
        )
        await drain_pending_usage_writes()

        assert tracker.total_calls == 0, "a flushed tracker must not be added to"
        assert ledger.call_writes == 1
        (key,) = ledger.daily
        assert key[2:5] == ("account", "u-turn", "u-turn")
        assert ledger.daily[key]["calls"] == 1
        assert ledger.daily[key]["input_tokens"] == 100

    async def test_no_turn_schedules_one_row_with_the_requests_actor(self, ledger):
        set_current_actor_user_id("u-request")
        record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0)
        await drain_pending_usage_writes()

        assert ledger.call_writes == 1
        (key,) = ledger.daily
        assert key[0] == STANDALONE_ENTERPRISE_ID
        assert key[2:5] == ("account", "u-request", "u-request")

    async def test_no_turn_and_no_actor_writes_an_unattributed_row(self, ledger):
        record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0)
        await drain_pending_usage_writes()

        (key,) = ledger.daily
        assert key[2:5] == (SUBJECT_NONE, "", "")

    async def test_the_billing_organization_is_the_payer(self, ledger):
        set_current_actor_user_id("u-request")
        set_current_billing_organization_id("org-7")
        record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0)
        await drain_pending_usage_writes()

        (key,) = ledger.daily
        assert key[2:5] == ("organization", "org-7", "u-request")


# =============================================================================
# Attribution
# =============================================================================


class TestANoneResponse:
    """A ``None`` response is recorded by neither arm: there was no call."""

    async def test_the_turn_arm_adds_nothing(self, ledger, unpersisted):
        tracker = TurnTokenTracker()
        _in_turn(
            tracker,
            lambda: record_provider_call("anthropic", "claude-sonnet-4-6", None, 1.0),
        )
        assert tracker.total_calls == 0 and not tracker.buckets
        assert not usage_ledger._pending_writes

    async def test_the_own_row_arm_writes_nothing(self, ledger, unpersisted):
        record_provider_call("anthropic", "claude-sonnet-4-6", None, 1.0)
        assert not usage_ledger._pending_writes
        await drain_pending_usage_writes()
        assert ledger.call_writes == 0 and not ledger.daily
        assert unpersisted.counts == {}


class TestAttribution:
    def test_standalone_is_a_tenant(self):
        assert capture_attribution("u1") == UsageAttribution(
            STANDALONE_ENTERPRISE_ID, "account", "u1", "u1"
        )

    def test_the_multi_tenant_sentinel_is_not(self, multi_tenant):
        assert capture_attribution("u1") is None

    def test_a_bound_enterprise_under_multi_is(self, multi_tenant):
        set_current_enterprise_id("ent-a")
        assert capture_attribution(None) == UsageAttribution(
            "ent-a", SUBJECT_NONE, "", ""
        )


# =============================================================================
# Failing open, counted
# =============================================================================


class TestFailingOpen:
    async def test_no_tenant_writes_nothing_and_is_counted(
        self, ledger, unpersisted, multi_tenant
    ):
        record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0)
        await drain_pending_usage_writes()
        assert ledger.call_writes == 0
        assert unpersisted.counts == {REASON_NO_TENANT: 1}

    async def test_no_tenant_on_a_turn_counts_every_call(
        self, ledger, unpersisted, multi_tenant
    ):
        tracker = TurnTokenTracker(attribution=capture_attribution("u1"))
        assert tracker.attribution is None

        def calls():
            for _ in range(3):
                record_provider_call(
                    "anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0
                )

        _in_turn(tracker, calls)
        await flush_turn(tracker, case_id="c1", turn_number=1, investigation_turn=1)
        assert ledger.turn_writes == 0
        assert unpersisted.counts == {REASON_NO_TENANT: 3}

    async def test_not_composed_is_counted_without_a_log_line(
        self, unpersisted, caplog
    ):
        with caplog.at_level(logging.DEBUG, logger=usage_ledger.__name__):
            record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0)
            await drain_pending_usage_writes()
        assert unpersisted.counts == {REASON_NOT_COMPOSED: 1}
        assert not [r for r in caplog.records if r.name == usage_ledger.__name__]

    async def test_a_store_error_is_counted_logged_and_swallowed(
        self, unpersisted, caplog
    ):
        class _Broken(InMemoryUsageLedger):
            async def record_call(self, attribution, bucket, usage_date):
                raise RuntimeError("INSERT … parameters: ('u-secret', 'model-x')")

        install_usage_ledger(_Broken())
        set_current_actor_user_id("u-secret")
        with caplog.at_level(logging.WARNING, logger=usage_ledger.__name__):
            # The caller sees nothing.
            record_provider_call("anthropic", "model-x", _Resp(100, 10), 1.0)
            await drain_pending_usage_writes()

        assert unpersisted.counts == {REASON_STORE_ERROR: 1}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "store_error" in message
        # Names the reason, never the row.
        assert "u-secret" not in message and "model-x" not in message

    async def test_a_failed_turn_flush_is_counted_per_call_and_never_raises(
        self, unpersisted, caplog
    ):
        class _Broken(InMemoryUsageLedger):
            async def record_turn(self, attribution, turn, buckets, usage_date):
                raise RuntimeError("database is locked")

        install_usage_ledger(_Broken())
        tracker = TurnTokenTracker(attribution=capture_attribution("u1"))

        def calls():
            record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(1, 1), 1.0)
            record_provider_call("openai", "gpt-4o", _Resp(1, 1), 1.0)

        _in_turn(tracker, calls)
        with caplog.at_level(logging.WARNING, logger=usage_ledger.__name__):
            await flush_turn(tracker, case_id="c1", turn_number=1, investigation_turn=1)

        assert unpersisted.counts == {REASON_STORE_ERROR: 2}
        assert tracker.flushed
        assert any("store_error" in r.getMessage() for r in caplog.records)

    async def test_an_own_row_whose_attribution_raises_is_counted(
        self, ledger, unpersisted, monkeypatch, caplog
    ):
        def _raise(_actor):
            raise RuntimeError("settings unreadable")

        monkeypatch.setattr(usage_ledger, "capture_attribution", _raise)
        with caplog.at_level(logging.WARNING, logger=usage_ledger.__name__):
            record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(1, 1), 1.0)
        assert unpersisted.counts == {REASON_ATTRIBUTION_ERROR: 1}
        assert ledger.call_writes == 0
        assert any(
            "attribution_error" in r.getMessage() and "RuntimeError" in r.getMessage()
            for r in caplog.records
        )

    async def test_a_turn_whose_attribution_failed_is_counted_as_such(
        self, ledger, unpersisted
    ):
        tracker = TurnTokenTracker(attribution=None, attribution_failed=True)
        _in_turn(
            tracker,
            lambda: record_provider_call(
                "anthropic", "claude-sonnet-4-6", _Resp(1, 1), 1.0
            ),
        )
        await flush_turn(tracker, case_id="c1", turn_number=1, investigation_turn=1)
        assert unpersisted.counts == {REASON_ATTRIBUTION_ERROR: 1}
        assert ledger.turn_writes == 0

    def test_no_running_loop_is_counted(self, ledger, unpersisted):
        # A synchronous caller: nothing to schedule the write on.
        record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(100, 10), 1.0)
        assert unpersisted.counts == {REASON_NO_LOOP: 1}
        assert ledger.call_writes == 0


# =============================================================================
# A cancelled write is counted, and stays cancelled
# =============================================================================


class _Hung(InMemoryUsageLedger):
    async def record_call(self, attribution, bucket, usage_date):
        await asyncio.sleep(60)

    async def record_turn(self, attribution, turn, buckets, usage_date):
        await asyncio.sleep(60)


class TestACancelledWrite:
    async def test_an_own_row_write(self, unpersisted):
        install_usage_ledger(_Hung())
        record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(1, 1), 1.0)
        (task,) = usage_ledger._pending_writes
        await asyncio.sleep(0)  # in flight
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled(), "the cancellation must not be swallowed"
        assert unpersisted.counts == {REASON_STORE_ERROR: 1}

    async def test_a_turn_flush(self, unpersisted):
        install_usage_ledger(_Hung())
        tracker = TurnTokenTracker(attribution=capture_attribution("u1"))

        def calls():
            for _ in range(2):
                record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(1, 1), 1.0)

        _in_turn(tracker, calls)
        task = asyncio.get_running_loop().create_task(
            flush_turn(tracker, case_id="c1", turn_number=1, investigation_turn=1)
        )
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert unpersisted.counts == {REASON_STORE_ERROR: 2}


# =============================================================================
# The flush
# =============================================================================


class TestTheFlush:
    async def test_one_turn_row_whose_totals_the_daily_rows_sum_to(self, ledger):
        tracker = TurnTokenTracker(
            actor_user_id="u1", attribution=capture_attribution("u1")
        )

        def calls():
            record_provider_call(
                "anthropic", "claude-sonnet-4-6", _Resp(1000, 100, 4000, 0), 1.0
            )
            record_provider_call(
                "anthropic",
                "claude-sonnet-4-6",
                _Resp(10, 1),
                1.0,
                outcome="low_confidence",
            )
            record_provider_call("openai", "gpt-4o", _Resp(300, 30), 1.0)

        _in_turn(tracker, calls)
        await flush_turn(tracker, case_id="c1", turn_number=7, investigation_turn=5)

        assert tracker.flushed
        assert ledger.turn_writes == 1
        row = ledger.turns[(STANDALONE_ENTERPRISE_ID, "c1", 7)]
        assert row["investigation_turn"] == 5
        assert row["actor_user_id"] == "u1"
        assert row["calls"] == 3
        assert row["low_confidence_calls"] == 1
        assert row["spend_weighted_tokens"] == tracker.spend_weighted_tokens
        assert len(ledger.daily) == 3
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "calls"):
            assert sum(r[name] for r in ledger.daily.values()) == row[name], name
        assert sum(
            r["estimated_cost_usd"] for r in ledger.daily.values()
        ) == pytest.approx(row["estimated_cost_usd"])

    async def test_a_turn_with_no_billed_call_writes_nothing(self, ledger):
        tracker = TurnTokenTracker(attribution=capture_attribution("u1"))
        await flush_turn(tracker, case_id="c1", turn_number=1, investigation_turn=1)
        assert tracker.flushed
        assert ledger.turn_writes == 0 and not ledger.daily


# =============================================================================
# The shutdown drain
# =============================================================================


class TestTheDrain:
    """The drain's deadline — it gives up at its timeout and counts what it
    abandoned — is pinned in virtual time, in
    ``test_usage_ledger_drain_deadline.py``."""

    async def test_awaits_pending_writes(self, ledger):
        class _Slow(InMemoryUsageLedger):
            async def record_call(self, attribution, bucket, usage_date):
                await asyncio.sleep(0.05)
                await super().record_call(attribution, bucket, usage_date)

        slow = _Slow()
        install_usage_ledger(slow)
        for _ in range(3):
            record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(1, 1), 1.0)
        assert slow.call_writes == 0

        assert await drain_pending_usage_writes(timeout_s=5) == 0
        assert slow.call_writes == 3
        assert not usage_ledger._pending_writes


def _a_task_left_by_a_closed_loop() -> "asyncio.Task[None]":
    """A write task that started on another loop, which then closed under it.

    Built on its own thread, because this test's loop is running on this one.
    """
    box: dict = {}

    def run() -> None:
        loop = asyncio.new_event_loop()

        async def hang() -> None:
            await asyncio.sleep(3600)

        box["task"] = loop.create_task(hang())
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return box["task"]


async def test_the_drain_drops_a_task_from_a_closed_loop(unpersisted):
    foreign = _a_task_left_by_a_closed_loop()
    assert not foreign.done()
    usage_ledger._pending_writes.add(foreign)
    try:
        assert await drain_pending_usage_writes(timeout_s=0.1) == 0
        assert foreign not in usage_ledger._pending_writes
        assert unpersisted.counts == {REASON_STORE_ERROR: 1}
    finally:
        usage_ledger._pending_writes.discard(foreign)


def test_schedule_call_write_is_the_one_path_for_an_own_row(ledger, unpersisted):
    """Guard against a second, unaccounted writer: outside a loop the one path
    counts and returns rather than writing synchronously."""
    from faultmaven.infrastructure.llm.usage_ledger import CallBucket

    schedule_call_write(CallBucket("anthropic", "m", "kept", calls=1), actor_user_id="")
    assert unpersisted.counts == {REASON_NO_LOOP: 1}


# =============================================================================
# The actor binding
# =============================================================================


class TestTheActorBinding:
    """``require_authentication`` records who the request acts as; nothing else
    does, and an unauthenticated route leaves it unset."""

    @staticmethod
    def _app(user):
        from faultmaven.api.v1.auth_dependencies import (
            get_current_user_optional,
            require_authentication,
        )

        app = FastAPI()

        @app.get("/authed")
        async def authed(_user=Depends(require_authentication)):
            return {"actor": get_current_actor_user_id()}

        @app.get("/open")
        async def open_route():
            return {"actor": get_current_actor_user_id()}

        async def _user():
            return user

        app.dependency_overrides[get_current_user_optional] = _user
        return app

    def _user(self):
        from datetime import datetime, timezone

        from faultmaven.modules.auth.domain.models.auth import DevUser

        return DevUser(
            user_id="user_actor_1",
            username="actor",
            email="actor@example.com",
            display_name="Actor",
            created_at=datetime.now(timezone.utc),
        )

    def test_an_authenticated_route_sees_the_users_id(self):
        client = TestClient(self._app(self._user()))
        assert client.get("/authed").json() == {"actor": "user_actor_1"}

    def test_an_unauthenticated_route_sees_none(self):
        client = TestClient(self._app(self._user()))
        # Authenticate once first, so a value leaking across requests would show.
        client.get("/authed")
        assert client.get("/open").json() == {"actor": None}
