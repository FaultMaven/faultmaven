"""``fm-backfill-case-outcome`` gives each stored case its first record (#2636).

Ruled 2026-11-09: computed once from the case as stored, a fold over no prior,
through an ``fm-*`` entrypoint, with no legacy read path. Driven here against
a real SQLite case repository, so a write is a real versioned write of the
progress blob (``save_progress``; that it keeps ``updated_at`` on both dialects
is ``tests/integration/test_case_outcome_backfill_repository_2636.py``). The
console-entrypoint and database-gate pins cover the command with every other
``fm-*`` command (``test_console_entrypoints.py``, ``test_database_gate.py``,
``test_commands_shut_the_container_down.py``).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.cli.backfill_case_outcome import EXIT_SKIPPED, backfill_case_outcome
from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.outcome import (
    compute_case_outcome,
    record_case_outcome,
)
from faultmaven.core.investigation.terminal_transitions import execute_user_closure
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import CaseState, FixWindowState, ProblemStatus
from tests.case_builders import inquiry_case
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _identified_case,
)

pytestmark = pytest.mark.unit

_PROVIDER = "faultmaven.providers.tenancy.factory.requested_tenant_provider"


@pytest.fixture
async def repository():
    from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
        SQLiteCaseRepository,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield SQLiteCaseRepository(session)
    await engine.dispose()


@pytest.fixture
async def stored(repository):
    """Two stored cases without a record: an identified cause, and an inquiry."""
    identified, _root, _hyp = _identified_case()
    inquiry = inquiry_case(user_id="u", enterprise_id="o", statement="it is slow")
    for case in (identified, inquiry):
        await repository.save(case)
    return {c.case_id: (await repository.get(c.case_id)) for c in (identified, inquiry)}


async def _run(
    repository, *, dry_run=False, enterprise_id=None, provider="single", gate=None
):
    with patch(_PROVIDER, return_value=provider):
        return await backfill_case_outcome(
            enterprise_id=enterprise_id,
            dry_run=dry_run,
            repository=repository,
            gate=gate,
        )


class _Gate:
    """The delete gate's lease read: ``busy`` case ids hold a lease, and
    ``broken`` makes the read fail."""

    def __init__(self, busy=(), broken=False):
        self.busy, self.broken, self.read = set(busy), broken, []

    async def work_in_flight(self, key):
        self.read.append(key)
        if self.broken:
            raise ConnectionError("redis is unreachable")
        return key[1] in self.busy


async def test_a_dry_run_writes_nothing(repository, stored, capsys):
    assert await _run(repository, dry_run=True) == 0
    for case_id, before in stored.items():
        after = await repository.get(case_id)
        assert after.progress.outcome is None
        assert after.version == before.version
    out = capsys.readouterr().out
    assert "Records would be written: 2" in out
    assert "Dry run — nothing was written" in out


async def test_yes_writes_each_record_from_the_case_as_stored(repository, stored):
    assert await _run(repository) == 0
    for case_id, before in stored.items():
        after = await repository.get(case_id)
        assert after.progress.outcome == compute_case_outcome(None, before)
        assert after.version == before.version + 1


async def test_a_second_run_writes_none(repository, stored, capsys):
    await _run(repository)
    versions = {i: (await repository.get(i)).version for i in stored}
    capsys.readouterr()
    assert await _run(repository) == 0
    assert {i: (await repository.get(i)).version for i in stored} == versions
    out = capsys.readouterr().out
    assert "Have one:     2 (left alone)" in out
    assert "Records written: 0" in out


async def test_multi_tenant_requires_an_enterprise(repository, stored, capsys):
    assert await _run(repository, provider="multi") == 1
    assert "--enterprise-id is required" in capsys.readouterr().out
    for case_id in stored:
        assert (await repository.get(case_id)).progress.outcome is None


async def test_multi_tenant_binds_the_enterprise_it_names(repository, stored):
    bind = AsyncMock(return_value=SimpleNamespace(name="Acme"))
    with patch("faultmaven.cli._tenant.bind_and_load_enterprise", bind):
        assert await _run(repository, provider="multi", enterprise_id="ent_1") == 0
    bind.assert_awaited_once_with("ent_1")
    for case_id in stored:
        assert (await repository.get(case_id)).progress.outcome is not None


async def test_a_case_that_raises_is_listed_and_the_run_goes_on(
    repository, stored, monkeypatch, capsys
):
    broken, healthy = list(stored)
    real = outcome_module.compute_case_outcome

    def _compute(prior, case, **kwargs):
        if case.case_id == broken:
            raise KeyError("a derivation that cannot read this case")
        return real(prior, case, **kwargs)

    monkeypatch.setattr(outcome_module, "compute_case_outcome", _compute)
    assert await _run(repository) == 1
    assert (await repository.get(broken)).progress.outcome is None
    assert (await repository.get(healthy)).progress.outcome is not None
    out = capsys.readouterr().out
    assert "Failed:       1" in out
    assert f"  - {broken}: KeyError" in out


async def test_a_case_with_work_in_flight_is_skipped_and_a_rerun_writes_it(
    repository, stored, capsys
):
    busy, idle = list(stored)
    gate = _Gate(busy={busy})
    assert await _run(repository, gate=gate) == EXIT_SKIPPED
    assert (await repository.get(busy)).progress.outcome is None
    assert (await repository.get(idle)).progress.outcome is not None
    out = capsys.readouterr().out
    assert "Skipped:      1 (work in flight; re-run to write them)" in out
    assert f"  - {busy}: a turn or its task is in flight" in out
    assert ("o", busy) in gate.read

    gate.busy.clear()
    assert await _run(repository, gate=gate) == 0
    assert (await repository.get(busy)).progress.outcome is not None


async def test_a_lease_that_cannot_be_read_skips_the_case(repository, stored, capsys):
    assert await _run(repository, gate=_Gate(broken=True)) == EXIT_SKIPPED
    for case_id in stored:
        assert (await repository.get(case_id)).progress.outcome is None
    assert "its lease could not be read" in capsys.readouterr().out


async def test_a_lost_version_race_is_retried_from_a_fresh_load(repository, stored):
    case_id = next(iter(stored))
    real = repository.save_progress
    calls = []

    async def _racing(case):
        calls.append(case.version)
        if len(calls) == 1:
            # A turn saves between the backfill's load and its write.
            await repository.save(await repository.get(case.case_id))
        return await real(case)

    repository.save_progress = _racing
    assert await _run(repository) == 0
    after = await repository.get(case_id)
    assert after.progress.outcome is not None
    assert calls[1] == calls[0] + 1, "the second write carries the fresh version"


def _boom(*_a, **_k):
    raise RuntimeError("the record write raised")


async def _closed_with_an_open_record(repository, monkeypatch):
    """A case closed while its record write raised: the record it keeps was
    folded at the last cycle, under the open state."""
    case, _root, _hyp = _identified_case()
    record_case_outcome(case, at="cycle")
    # The evidence then shows the problem was never there: the close records
    # a false alarm, which the open record's closure does not say.
    case.progress.problem_status = ProblemStatus.INVALIDATED

    with monkeypatch.context() as patched:
        patched.setattr(outcome_module, "compute_case_outcome", _boom)
        execute_user_closure(case, case.user_id)
    assert case.state == CaseState.CLOSED
    assert case.progress.outcome.folded_under == CaseState.INVESTIGATING, "premise"
    assert case.progress.outcome.closure.reason != case.closure_reason, "premise"
    await repository.save(case)
    return case.case_id


async def test_a_terminal_record_no_terminal_write_folded_is_repaired(
    repository, monkeypatch, capsys
):
    case_id = await _closed_with_an_open_record(repository, monkeypatch)
    assert await _run(repository) == 0
    after = await repository.get(case_id)
    assert after.progress.outcome.folded_under == CaseState.CLOSED
    assert after.progress.outcome.closure.reason == after.closure_reason
    assert after.closure_reason == "closed_false_alarm"
    out = capsys.readouterr().out
    assert "Records written: 1 (1 repaired)" in out
    assert f"  - {case_id}: a terminal record no terminal write folded" in out


async def test_an_ordinary_terminal_record_is_left_alone(repository, capsys):
    case, _root, _hyp = _identified_case()
    execute_user_closure(case, case.user_id)
    assert case.progress.outcome.folded_under == CaseState.CLOSED, "premise"
    await repository.save(case)
    version = (await repository.get(case.case_id)).version
    assert await _run(repository) == 0
    assert (await repository.get(case.case_id)).version == version
    assert "Have one:     1 (left alone)" in capsys.readouterr().out


async def test_the_delete_gate_reads_a_turns_lease(repository, stored):
    """The real lease a turn holds (``hold_case``), read by the gate the
    container composes, in FakeRedis as standalone keeps it."""
    import fakeredis.aioredis as fakeredis_aio

    from faultmaven.infrastructure.concurrency.inflight import (
        CaseDeleteGate,
        hold_case,
    )

    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    gate = CaseDeleteGate(redis)
    busy = next(iter(stored))
    hold = await hold_case(redis, ("o", busy))
    try:
        assert await gate.work_in_flight(("o", busy)) is True
        assert await _run(repository, gate=gate) == EXIT_SKIPPED
    finally:
        await hold.release()
    assert await gate.work_in_flight(("o", busy)) is False
    assert await CaseDeleteGate(None).work_in_flight(("o", busy)) is False
    assert await _run(repository, gate=gate) == 0


async def test_a_repair_folds_over_the_stored_record_and_keeps_a_failed_window(
    repository, monkeypatch
):
    """#2595's shape: a FAILED window is carried, a re-root makes the fresh
    read UNKNOWN, the closed write raises. The repair keeps FAILED, carried
    from the stored record, and the closed case's closure."""
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
        _deepen,
        _eid,
        _fix,
        _recompute_at,
        _relapse,
        _window,
    )

    case, root, hyp = _identified_case()
    fix = _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    record_case_outcome(case, at="cycle")
    _deepen(case, hyp, root)
    record_case_outcome(case, at="cycle")
    assert _window(case.progress.outcome, fix.action_id).state == FixWindowState.FAILED
    assert compute_case_outcome(None, case).attempts[0].window.state != (
        FixWindowState.FAILED
    ), "premise: the fresh read is not FAILED"
    with monkeypatch.context() as patched:
        patched.setattr(outcome_module, "compute_case_outcome", _boom)
        execute_user_closure(case, case.user_id)
    assert case.progress.outcome.folded_under == CaseState.INVESTIGATING, "premise"
    await repository.save(case)

    assert await _run(repository) == 0
    after = await repository.get(case.case_id)
    assert after.progress.outcome.folded_under == CaseState.CLOSED
    assert _window(after.progress.outcome, fix.action_id).state == (
        FixWindowState.FAILED
    )
    assert after.progress.outcome.closure.reason == after.closure_reason


async def test_a_busy_rerun_on_recorded_cases_skips_nothing(repository, stored, capsys):
    await _run(repository)
    capsys.readouterr()
    gate = _Gate(busy=set(stored))
    assert await _run(repository, gate=gate) == 0
    assert gate.read == [], "a recorded case's lease is not read"
    out = capsys.readouterr().out
    assert "Skipped" not in out
    assert "Have one:     2 (left alone)" in out


# ---------------------------------------------------------------------------
# #2689: counted once, after the save; a dry run counts nothing
# ---------------------------------------------------------------------------


def _counted(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        outcome_module,
        "case_outcome_conflicts_total",
        SimpleNamespace(
            labels=lambda field: SimpleNamespace(inc=lambda: seen.append(field))
        ),
    )
    return seen


async def _repair_with_a_disagreement(repository, monkeypatch):
    """A case closed while its record write raised, whose stored record
    carries an attempt accepted at 6 the case now dates to 5: the repair's
    fold reads a real disagreement."""
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import _fix

    case, _root, _hyp = _identified_case()
    fix = _fix(case)
    record_case_outcome(case, at="cycle")
    fix.accepted_in_turn = 5
    with monkeypatch.context() as patched:
        patched.setattr(outcome_module, "compute_case_outcome", _boom)
        execute_user_closure(case, case.user_id)
    assert case.progress.outcome.folded_under == CaseState.INVESTIGATING, "premise"
    await repository.save(case)
    return case.case_id


async def test_2689_a_dry_run_counts_nothing(repository, monkeypatch):
    await _repair_with_a_disagreement(repository, monkeypatch)
    counted = _counted(monkeypatch)
    assert await _run(repository, dry_run=True) == 0
    assert counted == []
    assert await _run(repository) == 0
    assert counted == ["attempts.accepted_in_turn"], "positive control: written"


async def test_2689_a_retried_write_counts_each_disagreement_once_after_the_save(
    repository, monkeypatch
):
    case_id = await _repair_with_a_disagreement(repository, monkeypatch)
    counted = _counted(monkeypatch)
    real = repository.save_progress
    before_each_write = []

    async def _racing(case):
        before_each_write.append(list(counted))
        if len(before_each_write) == 1:
            # A turn saves between the backfill's load and its write.
            await repository.save(await repository.get(case.case_id))
        return await real(case)

    repository.save_progress = _racing
    assert await _run(repository) == 0
    assert before_each_write == [[], []], "nothing counted before a save"
    assert counted == ["attempts.accepted_in_turn"]
    after = await repository.get(case_id)
    assert [k.split(":")[0] for k in after.progress.outcome.disagreements] == [
        "attempts.accepted_in_turn"
    ]
