"""#2689 under counting rule (i) (#2636 B2, ruled 2026-11-13: "stored" means
the save has committed): each disagreement on the case outcome record is
counted once, when a saved record first carries it.

The turn counts the committed record's disagreement keys beyond those of the
record it loaded, and the narration's conflicts, inside ``settle_turn`` right
after the commit; the REST close after its retry helper returns. A failed or
cancelled commit and a retried attempt count nothing extra. Driven through
the REAL service and engine on a SQLite file database (``#1882``'s world), the
LLM stubbed, with a real disagreement: an attempt's carried
``accepted_in_turn`` the case no longer reads.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.milestone_engine import engine as engine_module
from faultmaven.core.investigation.milestone_engine.turn_commit import (
    TurnCommitPlan,
)
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.modules.agent.domain.services.investigation_service.turn_settlement import (
    PreparedTurn,
    settle_turn,
)
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.exceptions import StaleCaseException
from tests.case_builders import with_open_offer
from tests.unit.core.investigation.test_case_outcome_fold_2636 import _fix
from tests.unit.modules.agent.test_one_commit_per_turn_1882 import (  # noqa: F401
    CASE_ID,
    USER_ID,
    _committed,
    _confirm_click,
    _engine,
    _investigating_case,
    _seed,
    _service,
    _SlowCommit,
    _turn,
    sessions,
)

pytestmark = pytest.mark.unit

_FIELD = "attempts.accepted_in_turn"


@pytest.fixture
def counted(monkeypatch) -> list:
    """The conflict counter's increments, by field."""
    seen: list = []
    monkeypatch.setattr(
        outcome_module,
        "case_outcome_conflicts_total",
        SimpleNamespace(
            labels=lambda field: SimpleNamespace(inc=lambda: seen.append(field))
        ),
    )
    return seen


def _disagreeing(case):
    """``case`` with a stored record that carries an attempt accepted at turn
    3, and the case re-dating it to 2: every fold from here reads the carried
    verdict disagreeing (a real disagreement, counted on ``main`` at fold
    time)."""
    action = _fix(case, turn=3)
    case.progress.outcome = compute_case_outcome(None, case)
    action.accepted_in_turn = 2
    return case


def _key(case) -> str:
    (action,) = [a for a in case.proposed_actions if a.state == "accepted"]
    return f"{_FIELD}:{action.action_id}"


async def test_a_turn_whose_commit_fails_counts_nothing_and_the_next_counts_once(
    sessions, counted
):
    case = _disagreeing(_investigating_case())
    await _seed(sessions, case)

    with pytest.raises(StaleCaseException):
        await _turn(sessions, "what does the pool log show?", lose_the_race=True)
    assert counted == [], "the record never committed"
    assert (
        _key(case)
        not in (await _committed(sessions))["case"].progress.outcome.disagreements
    )

    await _turn(sessions, "what does the pool log show?")
    assert counted == [_FIELD]
    committed = (await _committed(sessions))["case"]
    assert _key(case) in committed.progress.outcome.disagreements

    await _turn(sessions, "and now?")
    assert counted == [_FIELD], "a record that carries it already counts nothing"


async def test_a_turn_cancelled_while_its_settlement_commits_counts_once(
    sessions, counted
):
    """The settlement runs on under its shield after the caller is cancelled:
    the turn commits, and it is counted there."""
    await _seed(sessions, _disagreeing(_investigating_case()))
    async with sessions() as session:
        repository = _SlowCommit(session, sessions)
        service = _service(repository, _engine(repository))
        prepared = await service.prepare_turn(
            case_id=CASE_ID,
            user_id=USER_ID,
            payload=TurnPayload(query="what does the pool log show?"),
        )
        gate = prepared.plan.gate()
        caller = asyncio.ensure_future(service.commit_turn(prepared))
        await repository.entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert counted == [], "nothing before the commit"
        repository.proceed.set()
        await asyncio.wait_for(gate, timeout=5)
        for _ in range(5):
            await asyncio.sleep(0)
    assert counted == [_FIELD]


def _close_offered():
    case = _disagreeing(_investigating_case())
    with_open_offer(case, kind="close", basis="model", summary="Close it?")
    return case


async def test_a_chat_confirmed_close_counts_once_after_its_commit_and_not_on_a_409(
    sessions, counted
):
    """The step-0 close (``confirm_open_offer`` → ``_execute_closed_transition``)
    writes its record before the turn's metadata exists; it is counted by the
    commit's diff, once."""
    case = _close_offered()
    await _seed(sessions, case)

    with pytest.raises(StaleCaseException):
        await _turn(
            sessions, "Yes, close it.", intent=_confirm_click(case), lose_the_race=True
        )
    assert counted == []

    response, _repository = await _turn(
        sessions, "Yes, close it.", intent=_confirm_click(case)
    )
    assert response.case_state == CaseState.CLOSED
    committed = (await _committed(sessions))["case"]
    assert committed.progress.outcome.folded_under == CaseState.CLOSED
    assert counted == [_FIELD]


# ---------------------------------------------------------------------------
# The narration's conflicts ride the plan and count after the commit
# ---------------------------------------------------------------------------


def test_the_narrations_conflicts_ride_the_plan_uncounted(monkeypatch, counted):
    """A B1d scenario whose decision the stored record no longer states: the
    narration collects the conflict onto the turn's commit plan, and counts
    nothing before the commit."""
    from tests.unit.core.investigation import golden_cases_2636 as golden

    plans: list = []

    class _Plan(TurnCommitPlan):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            plans.append(self)

    monkeypatch.setattr(engine_module, "TurnCommitPlan", _Plan)
    golden.B1D_SCENARIOS["turn_request_ready_on_a_decaying_leg"]()
    (plan,) = plans
    assert plan.narration_conflicts == ["narration.request_ready"]
    assert counted == []


def _prepared(case, conflicts) -> PreparedTurn:
    return PreparedTurn(
        case=case,
        plan=TurnCommitPlan(narration_conflicts=list(conflicts)),
        response=MagicMock(),
        payload=TurnPayload(query="ok"),
        loaded_disagreements=outcome_module.disagreement_keys(case.progress.outcome),
    )


async def test_the_narrations_conflicts_count_once_after_the_commit(counted):
    case = _investigating_case()
    case.progress.outcome = compute_case_outcome(None, case)
    repository = SimpleNamespace(save=AsyncMock(return_value=case))
    await settle_turn(repository, None, _prepared(case, ["narration.request_ready"]))
    assert counted == ["narration.request_ready"]


async def test_the_narrations_conflicts_count_nothing_on_a_failed_commit(counted):
    case = _investigating_case()
    case.progress.outcome = compute_case_outcome(None, case)
    stale = StaleCaseException(case.case_id, expected_version=1, actual_version=2)
    repository = SimpleNamespace(save=AsyncMock(side_effect=stale))
    with pytest.raises(StaleCaseException):
        await settle_turn(
            repository, None, _prepared(case, ["narration.request_ready"])
        )
    assert counted == []


async def test_a_count_that_raises_never_fails_the_committed_turn(monkeypatch):
    def _broken(field):
        raise RuntimeError("the registry is gone")

    monkeypatch.setattr(
        outcome_module,
        "case_outcome_conflicts_total",
        SimpleNamespace(labels=_broken),
    )
    case = _investigating_case()
    case.progress.outcome = compute_case_outcome(None, case)
    repository = SimpleNamespace(save=AsyncMock(return_value=case))
    await settle_turn(repository, None, _prepared(case, ["narration.request_ready"]))
    repository.save.assert_awaited_once()


# ---------------------------------------------------------------------------
# The REST close: counted after its retry helper returns
# ---------------------------------------------------------------------------


async def test_a_rest_close_retried_after_a_conflict_counts_once(counted):
    """``close_case`` re-runs its closure on a fresh load after a version
    conflict: each attempt folds the disagreement, and it is counted once,
    on the record the saving attempt wrote."""
    from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
    from faultmaven.config.tenant_context import set_current_enterprise_id
    from faultmaven.modules.case.domain.services.case_service import CaseService
    from faultmaven.modules.case.infrastructure.case_repository import (
        InMemoryCaseRepository,
    )

    class _LosesOnce(InMemoryCaseRepository):
        """Loads a copy, as a database does; the close's first write loses."""

        saves = 0

        async def get(self, case_id):
            stored = await super().get(case_id)
            return stored.model_copy(deep=True) if stored is not None else None

        async def save(self, case, **rows):
            self.saves += 1
            if self.saves == 2:  # the close's first write
                raise StaleCaseException(case.case_id, case.version, case.version + 1)
            return await super().save(case, **rows)

    case = _disagreeing(_investigating_case())
    set_current_enterprise_id(case.enterprise_id)
    try:
        repository = _LosesOnce()
        await repository.save(case)
        closed = await CaseService(repository).close_case(CASE_ID, USER_ID)
    finally:
        set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    assert repository.saves == 3, "premise: the first write lost the race"
    assert closed.state == CaseState.CLOSED
    assert _key(case) in closed.progress.outcome.disagreements
    assert counted == [_FIELD]


# ---------------------------------------------------------------------------
# The diff is over (key, first-read turn) pairs; each increment guarded
# ---------------------------------------------------------------------------


def test_a_disagreement_dropped_and_read_again_within_one_turn_counts_again(counted):
    """Stored at turn 4; at turn 9 one fold reads it gone and a later fold of
    the same turn reads it again: a return, counted once more."""
    case = _investigating_case()
    action = _fix(case, turn=3)
    first = compute_case_outcome(None, case)
    action.accepted_in_turn, case.current_turn = 2, 4
    stored = compute_case_outcome(first, case)
    assert list(stored.disagreements.values()) == [4], "premise: stored at 4"
    loaded = outcome_module.disagreement_keys(stored)
    case.current_turn = 9
    action.accepted_in_turn = 3
    gone = compute_case_outcome(stored, case)
    assert gone.disagreements == {}, "premise: read gone"
    action.accepted_in_turn = 2
    back = compute_case_outcome(gone, case)
    assert list(back.disagreements) == list(stored.disagreements)
    outcome_module.report_saved_disagreements(case.case_id, loaded, back)
    assert counted == [_FIELD]


def test_a_field_that_cannot_be_counted_drops_none_of_the_rest(monkeypatch):
    seen: list = []

    def _labels(field):
        if field not in {_FIELD, "narration.request_ready"}:
            raise ValueError(f"undeclared label {field}")
        return SimpleNamespace(inc=lambda: seen.append(field))

    monkeypatch.setattr(
        outcome_module,
        "case_outcome_conflicts_total",
        SimpleNamespace(labels=_labels),
    )
    case = _investigating_case()
    saved = compute_case_outcome(None, case).model_copy(
        update={"disagreements": {"aaa.unknown:x": 9, f"{_FIELD}:act": 9}}
    )
    outcome_module.report_saved_disagreements(
        case.case_id, frozenset(), saved, ("narration.request_ready",)
    )
    assert seen == [_FIELD, "narration.request_ready"]


# ---------------------------------------------------------------------------
# Only this settlement's own commit counts
# ---------------------------------------------------------------------------


async def test_a_commit_only_the_receipt_probe_vouches_for_counts_nothing(
    monkeypatch, counted
):
    """The duplicate-key path (#1888 A3): this settlement's own commit raised,
    and the receipt read back under its key and turn may be another
    request's commit. The turn answers as committed and counts nothing; the
    same save that commits counts its disagreement (control)."""
    import contextlib

    from faultmaven.modules.case.domain.owned_models.turn_receipt import (
        TurnReceipt,
    )
    from faultmaven.modules.case.infrastructure import (
        sessionless_case_repository as sessionless,
    )

    class _Dialect:
        raises = True

        async def save(self, case, **rows):
            if self.raises:
                raise ConnectionError("lost mid-COMMIT")
            return case

    dialect = _Dialect()

    @contextlib.asynccontextmanager
    async def _session():
        yield None

    monkeypatch.setattr(sessionless, "get_db_session", _session)
    monkeypatch.setattr(sessionless, "get_repository_for_session", lambda _s: dialect)
    repository = sessionless.SessionlessCaseRepository()
    monkeypatch.setattr(repository, "_receipt_committed", AsyncMock(return_value=True))

    async def _settle():
        case = _disagreeing(_investigating_case())
        prepared = _prepared(case, ["narration.request_ready"])
        prepared.plan.receipt = TurnReceipt(
            case_id=case.case_id,
            author_id=USER_ID,
            idempotency_key="k" * 16,
            request_fingerprint="f" * 64,
            turn_number=case.current_turn,
            response={},
        )
        compute = outcome_module.compute_case_outcome
        case.progress.outcome = compute(case.progress.outcome, case)
        await settle_turn(repository, None, prepared)

    await _settle()
    assert counted == [], "vouched for by the receipt alone"
    dialect.raises = False
    await _settle()
    assert counted == [_FIELD, "narration.request_ready"], "control: its own commit"
