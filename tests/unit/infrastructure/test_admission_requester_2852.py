"""Admission with a bound requester (#2852, Principle 14; the rulings of
2026-11-19).

``admission_principal`` keys the bound account's jobs ``(account, requester)``
and every other job ``(principal, None)``. ``FairSlots`` applies ``main``'s
rule to the account, per pool, and its requesters take turns among that
account's own waiters: fewest held, then least recently granted since the
account was idle, then earliest, a grant taking the account's earliest place.
Each of the four ``FairSlots`` users acquires and releases one key.
"""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest

from faultmaven.config.tenant_context import bind_admission_requester
from faultmaven.infrastructure import embedding_guard, worker_process
from faultmaven.infrastructure.worker_process import FairSlots, admission_principal
from faultmaven.modules.knowledge.domain.services import (
    document_preprocessor,
    runbook_gate,
)
from tests.unit.infrastructure.test_fair_slots_2645 import (
    _LOGS,
    _settle,
    _tier1_service,
)

pytestmark = [pytest.mark.unit]

ACCOUNT, REQUESTER = "svc-slack", "r" * 32
BOUND, UNBOUND = (ACCOUNT, REQUESTER), (ACCOUNT, None)


@pytest.fixture(autouse=True)
def _unbound():
    bind_admission_requester(None)
    yield
    bind_admission_requester(None)


def test_the_key_names_the_requester_only_for_its_bound_account():
    assert admission_principal(ACCOUNT) == UNBOUND
    bind_admission_requester(ACCOUNT, REQUESTER)
    assert admission_principal(ACCOUNT) == BOUND
    gate = runbook_gate.SYSTEM_GATE_PRINCIPAL
    assert admission_principal(gate) == (gate, None)
    bind_admission_requester(None)
    assert admission_principal(ACCOUNT) == UNBOUND


async def test_an_account_holding_a_colon_is_its_own_account():
    """The key is structured: ``("a:b", None)`` and ``("a", "b")`` are two
    accounts, so the second is a newcomer the one free slot admits."""
    slots = FairSlots(2)
    await slots.acquire("a:b")
    second = asyncio.create_task(slots.acquire("a", "b"))
    await _settle()
    assert second.done()


# ---------------------------------------------------------------------------
# The account rule and the two-level grant
# ---------------------------------------------------------------------------


class _Pool:
    """Jobs on real ``FairSlots``: each records its grant, and a release ends
    the account's earliest-granted running job."""

    def __init__(self, count: int) -> None:
        self.slots, self.grants, self.running = FairSlots(count), [], {}
        self.tasks: list = []
        self.who: dict = {}

    def submit(self, job: int, account: str, requester) -> None:
        self.who[job] = requester

        async def run() -> None:
            await self.slots.acquire(account, requester)
            self.grants.append((account, job))
            self.running.setdefault(account, []).append((job, requester))

        self.tasks.append(asyncio.create_task(run()))

    def finish(self, account: str):
        if self.running.get(account):
            _, requester = self.running[account].pop(0)
            self.slots.release(account, requester)
            return requester

    def order(self) -> str:
        return " ".join(self.who[job] for _, job in self.grants)

    def concurrent(self, account: str) -> list:
        return [r for _, r in self.running.get(account, [])]


async def _replay(count: int, schedule, with_requesters: bool):
    pool = _Pool(count)
    for job, (op, account, requester) in enumerate(schedule):
        if op == "submit":
            pool.submit(job, account, requester if with_requesters else None)
        else:
            pool.finish(account)
        await _settle()
    for task in pool.tasks:
        task.cancel()
    return pool.grants


def _schedule(seed: int, steps: int = 120):
    rng, schedule = random.Random(seed), []
    for _ in range(steps):
        account = rng.choice("ABC")
        if rng.random() < 0.55:
            requester = None if account == "C" else rng.choice(["r1", "r2", "r3"])
            schedule.append(("submit", account, requester))
        else:
            schedule.append(("finish", account, None))
    return schedule


@pytest.mark.parametrize("count", [1, 2, 3, 4])
async def test_requesters_only_permute_an_accounts_own_grants(count):
    """Replayed with and without requester ids, every account is granted at the
    same steps as under ``main``'s rule (no ids: one key per account); only
    which of its own jobs runs changes."""
    permuted = False
    for seed in range(5):
        schedule = _schedule(seed)
        with_ids = await _replay(count, schedule, True)
        without = await _replay(count, schedule, False)
        assert [a for a, _ in with_ids] == [a for a, _ in without], (count, seed)
        permuted |= with_ids != without
    if count > 2:
        assert permuted, "no schedule exercised the requester order"


async def test_the_account_takes_its_earliest_place_on_a_cross_account_tie():
    """The schedule the ruling's probe found at the previous head: X granted a
    later requester and kept its earliest place, so it took Y's grant."""
    grants = {}
    for with_ids in (True, False):
        pool = _Pool(4)
        for job, (account, requester) in enumerate(
            [("X", "a"), ("X", "a"), ("Y", "y"), ("X", "a"), ("Y", "y"), ("X", "b")]
        ):
            pool.submit(job, account, requester if with_ids else None)
            await _settle()
        for _ in range(2):
            pool.finish("X")
            await _settle()
        grants[with_ids] = [account for account, _ in pool.grants]
    assert grants[True] == grants[False] == ["X", "X", "Y", "X", "Y"]


async def test_two_slots_take_turns_one_job_each():
    """The ruling's 2-slot consequence: A x4, B x2, C x1 queued on one account."""
    pool = _Pool(2)
    for job, requester in enumerate("AAAABBC"):
        pool.submit(job, "slack", requester)
    await _settle()
    while pool.running.get("slack"):
        pool.finish("slack")
        await _settle()
    assert pool.order() == "A B C A B A A"
    assert pool.slots._granted == {}, "the stamps outlived the account's idle"


async def test_two_one_job_requesters_never_starve_a_queued_batch():
    pool, job = _Pool(2), 0
    for requester in ["B"] * 5 + ["A1", "A2"]:
        pool.submit(job, "slack", requester)
        job += 1
    await _settle()
    for _ in range(14):
        done = pool.finish("slack")
        await _settle()
        if done != "B":  # a one-job-at-a-time requester sends its next job
            pool.submit(job, "slack", done)
            job += 1
            await _settle()
    assert pool.order().split()[:15] == ["B", "A1", "A2"] * 5


async def test_a_cancelled_waiter_releases_nothing_it_does_not_hold():
    pool = _Pool(2)
    for job, requester in enumerate(["u1", "u1", "u2"]):
        pool.submit(job, "slack", requester)
        await _settle()
    pool.finish("slack")  # u2 is served, taking the account's earliest place
    await _settle()
    assert pool.order() == "u1 u2"
    pool.tasks[1].cancel()  # u1's waiting job
    await _settle()
    assert dict(pool.slots._by_key) == {("slack", "u2"): 1}
    assert not pool.slots._waiting and "slack" in pool.slots._granted
    pool.finish("slack")
    assert pool.slots._granted == {} and pool.slots._free == 2


async def test_two_slots_serve_a_second_person_next_never_beside_the_first():
    pool = _Pool(2)
    pool.submit(0, "slack", "u1")
    await _settle()
    pool.submit(1, "slack", "u2")
    await _settle()
    assert pool.concurrent("slack") == ["u1"]
    pool.finish("slack")
    await _settle()
    pool.submit(2, "slack", "u1")  # the first person's next job
    await _settle()
    assert pool.concurrent("slack") == ["u2"]
    assert pool.grants == [("slack", 0), ("slack", 1)]


async def test_three_slots_run_a_second_person_beside_the_first_ahead_of_its_batch():
    pool = _Pool(3)
    for job in range(3):
        pool.submit(job, "slack", "u1")  # the first person's batch
    await _settle()
    pool.submit(3, "slack", "u2")
    await _settle()
    pool.finish("slack")
    await _settle()
    assert pool.grants == [("slack", 0), ("slack", 1), ("slack", 3)]
    assert sorted(pool.concurrent("slack")) == ["u1", "u2"]


# ---------------------------------------------------------------------------
# Each site acquires and releases one key
# ---------------------------------------------------------------------------


@pytest.fixture
def keys(monkeypatch):
    """Each site's acquired and released keys, through the real slots."""
    seen = {"acquired": [], "released": []}
    acquire, release = FairSlots.acquire, FairSlots.release

    async def acquiring(self, account, requester=None):
        seen["acquired"].append((account, requester))
        await acquire(self, account, requester)

    def releasing(self, account, requester=None):
        seen["released"].append((account, requester))
        release(self, account, requester)

    async def failing_run(*_args, **_kwargs):
        raise worker_process.WorkerFailed("no worker in this test")

    monkeypatch.setattr(FairSlots, "acquire", acquiring)
    monkeypatch.setattr(FairSlots, "release", releasing)
    monkeypatch.setattr(worker_process, "run", failing_run)
    return seen


async def _tier1():
    await _tier1_service().classify_and_extract(
        content=_LOGS, filename="x.log", principal=ACCOUNT
    )


async def _preparation(monkeypatch, tmp_path):
    async def prepared(*_args):
        return "prepared"

    monkeypatch.setattr(document_preprocessor, "_run_worker", prepared)
    await document_preprocessor._prepare(tmp_path / "a.md", "text/markdown", ACCOUNT)


async def _gate():
    with pytest.raises(runbook_gate.GateUnavailable):
        await runbook_gate.avalidate_and_score("# Runbook", principal=ACCOUNT)


async def _token_count():
    assert await embedding_guard.count_index_tokens(["a"], principal=ACCOUNT) is None


#: Tier 1 and the gate are reached under a binding (a turn's attachment or
#: reclassification, a Slack turn's runbook conversion). No declared route
#: reaches preparation or the token counts: their cases pin the key rule only.
SITES = {
    "tier1": lambda mp, tmp: _tier1(),
    "preparation": _preparation,
    "gate": lambda mp, tmp: _gate(),
    "token_count": lambda mp, tmp: _token_count(),
}


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
@pytest.mark.parametrize("site", sorted(SITES))
async def test_each_site_releases_the_key_it_acquired(
    site, bound, keys, monkeypatch, tmp_path
):
    if bound:
        bind_admission_requester(ACCOUNT, REQUESTER)
    await SITES[site](monkeypatch, tmp_path)
    expected = BOUND if bound else UNBOUND
    assert keys == {"acquired": [expected], "released": [expected]}


@pytest.mark.parametrize("creator", [ACCOUNT, "the-human-creator"])
async def test_a_conversion_keeps_the_requester_when_the_account_created_the_case(
    keys, creator
):
    """The runbook conversion a turn spawns (``runbook_creation``, a task
    created in the request's context) is admitted as the case's creator. It
    keeps the requester after the request unbinds only when the bound Slack
    account is that creator; a case handed to it as driver keeps its creator's
    key (#2852)."""
    from faultmaven.core.investigation.milestone_engine.runbook_creation import (
        RunbookCreator,
    )

    async def convert_from_case(*, request, user_id, enterprise_id):
        await runbook_gate.avalidate_and_score("# Runbook", principal=user_id)

    repository = SimpleNamespace(get=lambda _case_id: asyncio.sleep(0))
    creator_ = RunbookCreator(
        case_locks={"case-1": asyncio.Lock()},
        deps=SimpleNamespace(repository=repository),
    )
    committed = asyncio.get_running_loop().create_future()
    bind_admission_requester(ACCOUNT, REQUESTER)
    task = asyncio.create_task(
        creator_._run_runbook_conversion(
            SimpleNamespace(convert_from_case=convert_from_case),
            SimpleNamespace(case_id="case-1"),
            creator,
            "ent-1",
            committed=committed,
        )
    )
    bind_admission_requester(None)  # the next request on this task
    committed.set_result(None)
    await task
    expected = BOUND if creator == ACCOUNT else (creator, None)
    assert keys == {"acquired": [expected], "released": [expected]}
