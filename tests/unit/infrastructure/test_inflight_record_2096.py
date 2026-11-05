"""One record of the work in flight: drained per process, read per case (#2490, #2096).

The drain finishes or cancels what outlived its request before the stores
close; a cancellable task past the bound is cancelled, a settlement is awaited
to its end. The case lease, on FakeRedis as standalone runs it: a turn and a
delete each write their own marker before reading the other's, so they cannot
both pass, and a task tracked for a case holds its lease from the line that
spawns it.
"""

from __future__ import annotations

import asyncio
import threading

import fakeredis.aioredis as fakeredis_aio
import pytest

from faultmaven.core.preprocessing import vector_storage
from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.infrastructure.concurrency import inflight
from faultmaven.infrastructure.concurrency.inflight import (
    CaseDeleteGate,
    CaseDeleteInProgress,
    CaseWorkInProgress,
    Kind,
)
from faultmaven.infrastructure.concurrency.owned_claim import RELEASE_IF_HELD

pytestmark = pytest.mark.unit

CASE = ("ent_2096", "case_2096")


@pytest.fixture(autouse=True)
def _own_record(monkeypatch):
    """This test's record, so another test's leftovers are not drained here."""
    monkeypatch.setattr(inflight, "_TRACKED", {})
    monkeypatch.setattr(inflight, "_LEASES", {})
    monkeypatch.setattr(inflight, "_RELEASES", set())


async def _parked(gate: asyncio.Event, log: list, name: str) -> None:
    try:
        await gate.wait()
        log.append(f"{name} ended")
    except asyncio.CancelledError:
        log.append(f"{name} cancelled")
        raise


async def test_work_that_ends_within_the_bound_is_waited_for():
    log: list = []
    gate = asyncio.Event()
    task = inflight.track(
        asyncio.create_task(_parked(gate, log, "work")), what="w", kind=Kind.CANCELLABLE
    )
    asyncio.get_running_loop().call_soon(gate.set)

    assert await inflight.drain() == 0
    assert log == ["work ended"] and not task.cancelled()
    assert inflight.tracked() == set()


async def test_past_the_bound_cancellable_work_is_cancelled_and_a_settlement_is_not():
    """R1: a cancel must never reach a settlement's commit. The drain awaits it
    with no bound, and cancels only the cancellable rest."""
    log: list = []
    work_gate, settle_gate = asyncio.Event(), asyncio.Event()
    work = inflight.track(
        asyncio.create_task(_parked(work_gate, log, "work")),
        what="vectorization",
        kind=Kind.CANCELLABLE,
    )
    settlement = inflight.track(
        asyncio.create_task(_parked(settle_gate, log, "settlement")),
        what="settlement",
        kind=Kind.MUST_FINISH,
    )
    draining = asyncio.create_task(inflight.drain(timeout_s=0))
    for _ in range(10):
        await asyncio.sleep(0)

    assert work.cancelled() and log == ["work cancelled"]
    assert not settlement.done() and not draining.done()
    assert inflight.draining() is True
    settle_gate.set()
    assert await draining == 1
    assert log == ["work cancelled", "settlement ended"]
    assert inflight.draining() is False


async def test_another_loops_task_is_discarded_not_awaited():
    """A test's or a previous app's loop has closed: waiting on its task raises,
    and cancelling it raises too, so it is dropped (the ledger drain's shape)."""
    other = asyncio.new_event_loop()
    holder: dict = {}

    def _run_other():
        async def _spawn():
            holder["task"] = inflight.track(
                asyncio.ensure_future(asyncio.Event().wait()),
                what="elsewhere",
                kind=Kind.CANCELLABLE,
            )

        other.run_until_complete(_spawn())
        other.close()

    thread = threading.Thread(target=_run_other)
    thread.start()
    thread.join()

    assert await inflight.drain() == 0
    assert inflight.tracked() == set()
    assert not holder["task"].cancelled()


async def test_the_draining_flag_skips_the_evidence_vector_write(monkeypatch):
    """A cancel stops the task, not its thread: once the drain began, no
    vector write starts."""
    written: list = []

    class _Store:
        async def add_documents(self, **kwargs):
            written.append(kwargs["case_id"])

    async def _embed(texts, **_):
        return [[0.0] for _ in texts]

    monkeypatch.setattr(vector_storage, "embed_texts_or_raise", _embed)

    async def _store():
        return await vector_storage.store_in_vector_db_background(
            "c1", "ev1", "## A\nline\n", UnifiedDataType.LOGS, {}, _Store()
        )

    monkeypatch.setattr(inflight, "_DRAINS", [lambda: True])
    assert await _store() is vector_storage.VectorIndexOutcome.FAILED
    assert written == []
    monkeypatch.setattr(inflight, "_DRAINS", [])
    assert await _store() is vector_storage.VectorIndexOutcome.INDEXED
    assert written == ["c1"]


async def test_overlapping_drains_keep_the_flag_until_both_end():
    """The first drain to end must not clear the flag under the other."""
    finish = asyncio.Event()
    inflight.track(
        asyncio.create_task(finish.wait()), what="settlement", kind=Kind.MUST_FINISH
    )
    first = asyncio.create_task(inflight.drain())
    second = asyncio.create_task(inflight.drain())
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    assert inflight.draining() is True
    finish.set()
    await first
    assert inflight.draining() is False


async def test_a_hold_taken_during_the_drain_lowers_the_flag_for_good():
    """#2506: the flag stayed raised to the bound once a hold was taken. It
    reads the release's sticky predicate now ("no hold taken since this
    release began"), so it drops at once and stays down after the release."""
    holds = {"total": 0, "takes": 0}
    taken = holds["takes"]
    gate = asyncio.Event()
    work = inflight.track(
        asyncio.create_task(gate.wait()), what="w", kind=Kind.CANCELLABLE
    )
    draining = asyncio.create_task(
        inflight.drain(timeout_s=0.2, last=lambda: holds["takes"] == taken)
    )
    await asyncio.sleep(0)
    assert inflight.draining() is True

    holds["total"] += 1  # another composition takes a hold
    holds["takes"] += 1
    assert inflight.draining() is False
    holds["total"] -= 1  # and releases it before the drain ends
    assert inflight.draining() is False
    assert await asyncio.wait_for(draining, 10) == 0  # a hang guard
    assert not work.done()
    gate.set()
    await work


async def test_a_drain_still_the_last_keeps_the_flag_raised_to_its_end():
    """The control, as on main: with no hold taken, the flag stays raised while
    the drain runs, and past the bound the drain cancels the cancellable work."""
    gate = asyncio.Event()
    work = inflight.track(
        asyncio.create_task(gate.wait()), what="w", kind=Kind.CANCELLABLE
    )
    draining = asyncio.create_task(inflight.drain(timeout_s=0.2, last=lambda: True))
    await asyncio.sleep(0)
    while not draining.done():
        assert inflight.draining() is True
        await asyncio.sleep(0.01)

    assert await draining == 1
    assert work.cancelled() and inflight.draining() is False


async def test_another_live_loops_settlement_is_left_to_finish():
    """Not this drain's to cancel, from the wrong thread, mid-commit (R1)."""
    other, outcome, commit = asyncio.new_event_loop(), {}, threading.Event()

    def _run_other():
        async def _settle():
            try:
                await asyncio.get_running_loop().run_in_executor(None, commit.wait)
                outcome["settlement"] = "committed"
            except asyncio.CancelledError:
                outcome["settlement"] = "cancelled"

        async def _spawn():
            task = inflight.track(
                asyncio.ensure_future(_settle()), what="s", kind=Kind.MUST_FINISH
            )
            outcome["tracked"] = True
            await task

        other.run_until_complete(_spawn())
        other.close()

    thread = threading.Thread(target=_run_other)
    thread.start()
    while "tracked" not in outcome:
        await asyncio.sleep(0.01)

    try:
        drained, still = await inflight.drain(), len(inflight.tracked())
    finally:
        commit.set()
        thread.join()
    assert (drained, still, outcome["settlement"]) == (0, 1, "committed")


# ---------------------------------------------------------------------------
# The case lease
# ---------------------------------------------------------------------------


async def _live(redis) -> int:
    live, _left = await redis.eval(inflight.READ_LEASES, 1, inflight.lease_key(CASE))
    return int(live)


async def test_a_lease_renewed_past_its_initial_ttl_still_reads_one(monkeypatch):
    """``ZADD`` never re-arms a key's TTL: renewing the member alone let a long
    holder's whole key expire under it, and the delete then read nobody."""
    monkeypatch.setattr(inflight, "LEASE_TTL_MS", 600)
    monkeypatch.setattr(inflight, "LEASE_KEY_MARGIN_MS", 100)
    monkeypatch.setattr(inflight, "LEASE_RENEW_SECONDS", 0.15)
    redis = fakeredis_aio.FakeRedis(decode_responses=True)

    hold = await inflight.hold_case(redis, CASE)
    await asyncio.sleep(1.0)  # past the take's 600 ms and its key's 700 ms

    assert await _live(redis) == 1
    await hold.release()
    assert await _live(redis) == 0


async def test_every_turn_writes_its_member_so_a_lapsed_one_comes_back():
    """A member gone from Redis (an eviction, a failover) under a live hold: a
    turn that only joined the landed lease wrote nothing, and a delete then
    passed under both turns."""
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    first = await inflight.hold_case(redis, CASE)
    await redis.delete(inflight.lease_key(CASE))

    second = await inflight.hold_case(redis, CASE)

    with pytest.raises(CaseWorkInProgress):
        await CaseDeleteGate(redis).begin(CASE)
    await second.release()
    await first.release()


async def test_a_renewal_writes_a_lapsed_member_again(monkeypatch):
    monkeypatch.setattr(inflight, "LEASE_RENEW_SECONDS", 0.01)
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    hold = await inflight.hold_case(redis, CASE)
    await redis.delete(inflight.lease_key(CASE))

    async def _back():
        while not await _live(redis):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_back(), timeout=5)
    await hold.release()
    assert await _live(redis) == 0


async def test_a_renewals_rewrite_in_flight_lands_before_the_release(monkeypatch):
    """The rewrite was not among the writes the release waits for: still on
    the server at the release, it landed after the member's removal."""
    monkeypatch.setattr(inflight, "LEASE_RENEW_SECONDS", 0.01)
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    sent: list = []

    class _Server:
        """A rewrite once sent runs on the server, whoever stops waiting."""

        def __getattr__(self, command):
            return getattr(redis, command)

        async def eval(self, script, *args):
            sent.append(args[4])
            if args[4] != "" or len(sent) == 1:
                return await redis.eval(script, *args)

            async def _on_the_wire():
                await asyncio.sleep(0.05)
                return await redis.eval(script, *args)

            return await asyncio.shield(asyncio.ensure_future(_on_the_wire()))

    hold = await inflight.hold_case(_Server(), CASE)
    await redis.delete(inflight.lease_key(CASE))

    async def _rewrite_sent():
        while sent.count("") < 2:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_rewrite_sent(), timeout=5)
    await hold.release()
    await asyncio.sleep(0.1)  # past the rewrite's landing
    assert await redis.zcard(inflight.lease_key(CASE)) == 0


async def test_a_task_spawned_in_a_turn_holds_the_lease_before_its_first_step():
    """``spawn → route release → delete → task's first step``: the count rose
    when the task was tracked, so the turn's release removed no member, and the
    delete refuses with the task not yet started."""
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    started, finish = asyncio.Event(), asyncio.Event()

    async def _vectorize():
        started.set()
        await finish.wait()

    hold = await inflight.hold_case(redis, CASE)
    task = inflight.track(
        asyncio.create_task(_vectorize()),
        what="vectorization",
        case=CASE,
        kind=Kind.CANCELLABLE,
    )
    await hold.release()
    with pytest.raises(CaseWorkInProgress) as refused:
        await CaseDeleteGate(redis).begin(CASE)
    assert refused.value.retry_after >= 1
    assert not await redis.exists(inflight.tombstone_key(CASE))

    finish.set()
    await task
    await inflight.drain()
    tombstone = await CaseDeleteGate(redis).begin(CASE)
    await tombstone.end(deleted=False)
    assert started.is_set()


class _Sequenced:
    """FakeRedis whose named commands run only in ``order``: T1 the turn's
    lease take, T2 its tombstone read, D1 the delete's tombstone write, D2 its
    lease read, TR the turn's lease removal, DR the delete's tombstone release.
    A command named nowhere in ``order`` runs at once."""

    def __init__(self, redis, order):
        self._redis, self._order, self._next = redis, list(order), 0
        self._turn = asyncio.Condition()
        self._abandoned = False

    async def abandon(self):
        """Let every waiting command run: the sides deadlocked."""
        async with self._turn:
            self._abandoned = True
            self._turn.notify_all()

    def _name(self, command, args, kwargs):
        if command == "eval":
            script = args[0]
            if script == inflight.TAKE_OR_RENEW and args[5] == "":
                return "T1"
            return {inflight.READ_LEASES: "D2", RELEASE_IF_HELD: "DR"}.get(script)
        if command == "set" and kwargs.get("nx"):
            return "D1"
        return {"exists": "T2", "zrem": "TR"}.get(command)

    def __getattr__(self, command):
        call = getattr(self._redis, command)

        async def _in_order(*args, **kwargs):
            name = self._name(command, args, kwargs)
            if name not in self._order:
                return await call(*args, **kwargs)
            async with self._turn:
                await self._turn.wait_for(
                    lambda: self._abandoned or self._order[self._next] == name
                )
                result = await call(*args, **kwargs)
                self._next += 1
                self._turn.notify_all()
            return result

        return _in_order


async def _race(order):
    redis = _Sequenced(fakeredis_aio.FakeRedis(decode_responses=True), order)
    racing = asyncio.ensure_future(
        asyncio.gather(
            inflight.hold_case(redis, CASE),
            CaseDeleteGate(redis).begin(CASE),
            return_exceptions=True,
        )
    )
    if not (await asyncio.wait({racing}, timeout=5))[0]:
        await redis.abandon()
        await asyncio.wait({racing})
        pytest.fail(f"the sides never ran in the order {order}")
    turn, delete = racing.result()
    assert isinstance(turn, (inflight.CaseHold, CaseDeleteInProgress)), turn
    assert isinstance(delete, (inflight.CaseTombstone, CaseWorkInProgress)), delete
    if isinstance(turn, inflight.CaseHold):
        await turn.release()
    if isinstance(delete, inflight.CaseTombstone):
        await delete.end(deleted=False)
    return isinstance(turn, inflight.CaseHold), isinstance(
        delete, inflight.CaseTombstone
    )


@pytest.mark.parametrize(
    "order",
    [
        ("T1", "T2", "D1", "D2"),
        ("T1", "D1", "T2", "D2"),
        ("T1", "D1", "D2", "T2"),
        ("D1", "T1", "T2", "D2"),
        ("D1", "T1", "D2", "T2"),
        ("D1", "D2", "T1", "T2"),
    ],
    ids="-".join,
)
async def test_a_turn_and_a_delete_never_both_pass(order):
    turn_passed, delete_passed = await _race(order)

    assert not (turn_passed and delete_passed)
    if order[1] == "T2":
        assert turn_passed  # the turn read before any tombstone
    if order[1] == "D2":
        assert delete_passed  # the delete read before any lease


async def test_both_sides_can_refuse_and_that_is_safe():
    """The turn's lease lands, the delete's tombstone lands, each reads the
    other's: both refuse, nothing runs and nothing is cleared. A retry decides."""
    assert await _race(("T1", "D1", "T2", "D2", "TR", "DR")) == (False, False)
