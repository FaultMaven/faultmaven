"""The usage ledger's shutdown drain keeps its deadline (#640), in virtual time.

The drain's question is an ORDERING one — does it give up at its timeout and
count the write it abandoned, rather than hold shutdown hostage to a hung
database? — so it runs on ``tests.wallclock.VirtualTimeLoop`` (#1579), where
every ``asyncio.wait`` timeout costs exactly its nominal duration under any
load. A wall-clock threshold here would measure the scheduler, not the drain.

The drain reads no monotonic clock of its own: both of its waits are
``asyncio.wait(timeout=…)``, which run on the loop's clock, so nothing in
production needs pointing at ``virtual_time_module()``.

Its own module because the ``event_loop_policy`` override applies to every
test in the module that declares it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    set_current_actor_user_id,
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.infrastructure.llm import usage_ledger
from faultmaven.infrastructure.llm.metering import record_provider_call
from faultmaven.infrastructure.llm.usage_ledger import (
    REASON_STORE_ERROR,
    InMemoryUsageLedger,
    drain_pending_usage_writes,
    install_usage_ledger,
)
from tests.wallclock import VirtualTimePolicy, virtual_now

pytestmark = pytest.mark.unit

#: The drain's own grace after cancelling what its wait abandoned: the second
#: ``asyncio.wait(still, timeout=1.0)`` in ``drain_pending_usage_writes``.
_POST_CANCEL_GRACE_S = 1.0


@pytest.fixture
def event_loop_policy():
    """Every test here runs on a loop whose clock moves only when idle."""
    return VirtualTimePolicy()


@dataclass
class _Resp:
    input_tokens: int = 1
    output_tokens: int = 1
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tokens_used: int = 2
    prompt_cache_hit: bool = False
    model: str = "claude-sonnet-4-6"


class _RecordingCounter:
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


@pytest.fixture(autouse=True)
def clean_context():
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    set_current_actor_user_id(None)
    yield
    install_usage_ledger(None)


class _Hung(InMemoryUsageLedger):
    """A write that never finishes on its own, and dies when cancelled."""

    async def record_call(self, attribution, bucket, usage_date):
        await asyncio.sleep(3600)


class _Stubborn(InMemoryUsageLedger):
    """A write that swallows its first cancellation and keeps going."""

    async def record_call(self, attribution, bucket, usage_date):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)


async def test_the_drain_gives_up_at_its_timeout_and_counts_the_abandoned_write(
    unpersisted,
):
    """The wait ends at exactly the timeout. The cancellation reaches the write
    at once, so none of the post-cancel grace is spent, and the write counts
    itself as ``store_error`` on its way out."""
    install_usage_ledger(_Hung())
    record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(), 1.0)
    timeout_s = 0.1

    started = virtual_now()
    abandoned = await drain_pending_usage_writes(timeout_s=timeout_s)
    elapsed = virtual_now() - started

    assert elapsed == pytest.approx(timeout_s, abs=1e-9)
    assert abandoned == 1
    assert unpersisted.counts == {REASON_STORE_ERROR: 1}
    assert not usage_ledger._pending_writes


async def test_a_write_that_resists_cancellation_costs_at_most_the_grace(
    unpersisted,
):
    """The upper bound: a write that outlives its cancellation holds the drain
    for exactly the timeout plus the grace, and not a moment longer."""
    install_usage_ledger(_Stubborn())
    record_provider_call("anthropic", "claude-sonnet-4-6", _Resp(), 1.0)
    timeout_s = 0.1

    started = virtual_now()
    abandoned = await drain_pending_usage_writes(timeout_s=timeout_s)
    elapsed = virtual_now() - started

    assert elapsed == pytest.approx(timeout_s + _POST_CANCEL_GRACE_S, abs=1e-9)
    assert abandoned == 1
    for task in list(usage_ledger._pending_writes):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
