"""#2811 S2a: every saved case carries a record of the case as saved.

The record is written at the two save helpers, after the save's offer lapse
(``record_before_save``): a turn's commit (``commit_turn_plan``) and the case
service's write (``update_case_with_retry``). Only a cycle, a replay, a
resolve, a close or a first record mints a final verdict (``final_at``); any
other fold carries the finals already recorded. Stored records of an older
shape are upgraded on the fold and rewritten by the backfill. Driven through
the real engine, service and in-memory repository; the LLM is stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.milestone_engine.turn_commit import (
    TurnCommitPlan,
    commit_turn_plan,
)
from faultmaven.core.investigation.offers import lapse_offers
from faultmaven.core.investigation.outcome import (
    compute_case_outcome,
    narration_outcome,
    record_before_save,
    record_case_outcome,
)
from faultmaven.core.investigation.problem_status import propose_revision
from faultmaven.core.investigation.terminal_transitions import (
    execute_user_closure,
)
from faultmaven.modules.case.contracts import (
    CURRENT_OUTCOME_SHAPE,
    CaseOutcome,
    CaseState,
    ClosureEligibility,
    ClosureOutcome,
    ClosureReason,
    EvidenceCategory,
    FixWindowState,
    MitigationRefusalReason,
    ProblemStatus,
    WritePoint,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.case_builders import inquiry_case
from tests.unit.core.investigation.bound_rows import VERIFYING_ID, _row
from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
    _deepen,
    _fix,
    _relapse,
    _window,
)
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _offered_then_declined,
    _user_confirmation,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _eid,
    _identified_case,
    _recompute_at,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    REVISED,
    _case,
    _engine,
    _evidence,
    _with,
)

pytestmark = pytest.mark.unit

_SYMPTOM = EvidenceCategory.SYMPTOM_EVIDENCE


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


@pytest.fixture
def tenant():
    def bind(case):
        set_current_enterprise_id(case.enterprise_id)

    yield bind
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


def _matches(case) -> bool:
    """The saved record is the case's: folding the saved case over it once
    more changes nothing."""
    return narration_outcome(case) == case.progress.outcome


class _Copies(InMemoryCaseRepository):
    """Loads a copy, as a database does."""

    async def get(self, case_id):
        stored = await super().get(case_id)
        return stored.model_copy(deep=True) if stored is not None else None


async def _stored(case) -> InMemoryCaseRepository:
    repository = _Copies()
    await repository.save(case)
    return repository


def _revision_pending():
    """A verified case with a revision awaiting the user, its record written
    by the cycle that proposed it."""
    case = _case(ProblemStatus.VERIFIED)
    propose_revision(
        case,
        text=REVISED,
        evidence_ids=_with(case, _evidence(_SYMPTOM, "s9")),
        basis="the gateway log shows timeouts with normal DB latency",
        wording_key="revised",
    )
    record_case_outcome(case, at=WritePoint.CYCLE)
    assert case.progress.outcome.problem.status == ProblemStatus.REVISION_PENDING
    return case


# ---------------------------------------------------------------------------
# The save points
# ---------------------------------------------------------------------------


async def test_2658_the_menu_close_saves_a_record_of_the_case_as_saved():
    """Step 0's menu close moves the problem off REVISION_PENDING and opens
    the close offer without a cycle: its commit stores a record that says
    so, and the record narrated on that turn is the record stored."""
    case = _revision_pending()
    repository = await _stored(case)

    result = await _engine().process_turn(
        case=case,
        user_message="",
        intent_type="status_transition",
        intent_data={"to_state": "closed"},
        redaction_ctx=None,
    )
    narrated = narration_outcome(result["case_updated"])
    saved = await commit_turn_plan(
        repository, result["case_updated"], result["commit_plan"]
    )

    record = saved.progress.outcome
    assert saved.progress.problem_status == ProblemStatus.VERIFIED, "premise"
    assert record.problem.status == ProblemStatus.VERIFIED
    assert record.mitigation is None or (
        record.mitigation.refusal_reason != MitigationRefusalReason.REVISION_PENDING
    )
    assert _matches(saved)
    assert narrated == record


async def test_a_patch_statement_edit_saves_a_matching_record(tenant):
    case, _root, _hyp = _identified_case()
    record_case_outcome(case, at=WritePoint.CYCLE)
    tenant(case)
    repository = await _stored(case)

    await CaseService(repository).update_case(
        case.case_id, {"description": "checkout returns 503 under load"}
    )

    saved = await repository.get(case.case_id)
    assert saved.progress.outcome.problem.statement == "checkout returns 503 under load"
    assert _matches(saved)


async def test_an_inquiry_description_edit_refolds_the_record(tenant):
    """Outside an investigation the description is the statement in force:
    the scoped metadata write is followed by a fold of the record."""
    case = inquiry_case(user_id="u", enterprise_id="o", statement="it is slow")
    record_case_outcome(case, at=WritePoint.CYCLE)
    tenant(case)
    repository = await _stored(case)

    await CaseService(repository).update_case(
        case.case_id, {"description": "checkout is slow"}
    )

    saved = await repository.get(case.case_id)
    assert saved.description == "checkout is slow"
    assert saved.progress.outcome.problem.statement == "checkout is slow"
    assert _matches(saved)


async def test_a_terminal_state_written_raw_by_patch_is_refused(tenant):
    """PATCH's raw ``state``: a terminal value cannot pass the case's
    validators (a closed case needs ``closed_at`` and a reason), so the update
    is refused and nothing is saved; only the transitions close."""
    from faultmaven.exceptions import ServiceException

    case, _root, _hyp = _identified_case()
    record_case_outcome(case, at=WritePoint.CYCLE)
    tenant(case)
    repository = await _stored(case)
    before = (await repository.get(case.case_id)).version

    with pytest.raises(ServiceException):
        await CaseService(repository).update_case(case.case_id, {"state": "closed"})

    saved = await repository.get(case.case_id)
    assert saved.state == CaseState.INVESTIGATING
    assert saved.version == before


# ---------------------------------------------------------------------------
# The lapse runs first
# ---------------------------------------------------------------------------


async def test_the_record_reads_the_offers_as_the_save_lapses_them():
    """A decline given this turn has its premise widened by the save's lapse
    to cover the case as it stands: the stored record reads the decline
    standing, as the stored offer record does."""
    _engine_, case, _result = await _offered_then_declined()
    repository = await _stored(case)
    case.evidence.append(_user_confirmation(case.current_turn))
    unlapsed = compute_case_outcome(case.progress.outcome, case, at=WritePoint.COMMIT)
    assert unlapsed.resolution.declined_stands is False, "premise: the row is new"

    saved = await commit_turn_plan(repository, case, TurnCommitPlan())

    record = saved.progress.outcome
    assert record.resolution.declined_stands is True
    assert record.closure.reason == ClosureReason.RESOLUTION_DECLINED
    assert _matches(saved)


async def test_a_second_lapse_is_a_no_op():
    """The helper lapses, then the save's own lapse runs again: unchanged."""
    _engine_, case, _result = await _offered_then_declined()
    case.evidence.append(_user_confirmation(case.current_turn))
    first = lapse_offers(case)
    assert first != case.offer_record, "premise: the first lapse moves something"
    case.offer_record = first
    assert lapse_offers(case) == first


# ---------------------------------------------------------------------------
# No finals off-cycle
# ---------------------------------------------------------------------------


def _failed_after_a_cycle():
    """A fix whose window a cycle read UNKNOWN, then the relapse rows: the
    window now reads FAILED, first at a save that is not a cycle."""
    case, root, hyp = _identified_case()
    fix = _fix(case)
    record_case_outcome(case, at=WritePoint.CYCLE)
    assert _window(case.progress.outcome, fix.action_id).final_at is None
    _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    return case, root, hyp, fix


def test_a_failed_window_first_read_at_a_commit_is_not_final():
    case, root, hyp, fix = _failed_after_a_cycle()

    record_before_save(case, at=WritePoint.COMMIT)
    window = _window(case.progress.outcome, fix.action_id)
    assert window.state == FixWindowState.FAILED
    assert window.final_at is None, "a commit mints no final verdict"

    # The next cycle re-reads it: re-rooted, the window no longer fails, and
    # nothing is carried or counted as a disagreement.
    _deepen(case, hyp, root)
    record_case_outcome(case, at=WritePoint.CYCLE)
    record = case.progress.outcome
    assert _window(record, fix.action_id).state != FixWindowState.FAILED
    assert record.disagreements == {}


def test_the_next_cycle_mints_a_failure_it_still_reads():
    case, _root, _hyp, fix = _failed_after_a_cycle()
    record_before_save(case, at=WritePoint.COMMIT)
    record_case_outcome(case, at=WritePoint.CYCLE)
    final = _window(case.progress.outcome, fix.action_id).final_at
    assert (final.at, final.turn) == ("cycle", case.current_turn)


def test_no_off_cycle_fold_adds_a_final_marker():
    case, _root, _hyp, fix = _failed_after_a_cycle()
    for at in ("commit", "write", "metadata", "narration"):
        folded = compute_case_outcome(case.progress.outcome, case, at=at)
        assert _window(folded, fix.action_id).final_at is None, at
        assert all(a.final_at == b.final_at for a, b in _paired(case, folded)), at


def _paired(case, folded):
    prior = {a.action_id: a for a in case.progress.outcome.attempts}
    return [(prior[a.action_id], a) for a in folded.attempts if a.action_id in prior]


# ---------------------------------------------------------------------------
# The terminal carry and idempotence
# ---------------------------------------------------------------------------


async def test_the_entrys_closure_survives_a_terminal_turns_commit():
    case, _root, _hyp = _identified_case()
    execute_user_closure(case, "u")
    decided = ClosureOutcome(
        eligibility=ClosureEligibility.HAS_SUBSTANCE,
        reason=case.closure_reason,
        statement="the closure the entry decided",
    )
    record_case_outcome(case, at=WritePoint.CLOSED, closure=decided)
    repository = await _stored(case)

    saved = await commit_turn_plan(repository, case, TurnCommitPlan())

    assert saved.progress.outcome.closure == decided
    assert _matches(saved)


async def test_a_cycle_then_a_commit_is_one_fold():
    """A turn that runs the cycle: its commit refolds the cycle's record and
    changes nothing, the narrated re-check marks (``mark_told``) included."""
    _engine_, case, _result = await _offered_then_declined()
    cycled = case.progress.outcome
    repository = await _stored(case)
    saved = await commit_turn_plan(repository, case, TurnCommitPlan())
    assert saved.progress.outcome == cycled


async def test_mark_told_then_commit_keeps_the_mark():
    """The narration marks an unbound re-check told on the record
    (``mark_told``); the commit's fold carries the mark."""
    from tests.unit.core.investigation.test_turn_reorder_2636 import (
        _decline,
        _held_recheck,
        _live,
    )

    eng, case = _held_recheck()
    case.current_turn = 8
    _decline(case)
    await _live(eng, case, 8)
    told = case.progress.outcome.unbound_rechecks
    assert told and all(u.narrated_turn == 8 for u in told), "premise: told"
    repository = await _stored(case)

    saved = await commit_turn_plan(repository, case, TurnCommitPlan())

    assert saved.progress.outcome.unbound_rechecks == told
    assert _matches(saved)


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def _old_shape(record: CaseOutcome) -> CaseOutcome:
    """``record`` as a shape-1 record was stored: none of the fields #2811
    added, read back through the model."""
    raw = record.model_dump(mode="json")
    raw.pop("shape")
    for attempt in raw["attempts"]:
        attempt.pop("final_at")
        for key in ("relief_turn", "present_turn"):
            attempt.pop(key)
        if attempt["window"]:
            for key in ("final_at", "observed"):
                attempt["window"].pop(key)
            attempt["window"]["problem_row_ids"] = []
    for key in ("recheck", "posited_turn", "latest_disconfirmation_turn"):
        raw["cause"].pop(key)
    return CaseOutcome.model_validate(raw)


def test_an_old_shape_record_reads_shape_one_and_its_failures_carry_upgraded():
    case, root, hyp = _identified_case()
    fix = _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    old = _old_shape(compute_case_outcome(None, case, at=WritePoint.CYCLE))
    assert old.shape == 1
    # A relief recorded after the failure: the fresh read holds it, the
    # frozen verdict does not.
    _row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=11,
        evidence_id=_eid("late"),
        summary="checkout answers 200 later",
        rechecks=VERIFYING_ID,
    )
    fresh_rows = _window(
        compute_case_outcome(None, case, at=WritePoint.CYCLE), fix.action_id
    )
    assert fresh_rows.problem_row_ids == [_eid("relief"), _eid("late")], "premise"

    record = compute_case_outcome(old, case, at=WritePoint.COMMIT)

    window = _window(record, fix.action_id)
    assert record.shape == CURRENT_OUTCOME_SHAPE
    assert window.state == FixWindowState.FAILED
    assert window.observed is True
    assert window.final_at.at == "shape"
    assert window.problem_row_ids == [_eid("relief")]
    (attempt,) = [a for a in record.attempts if a.action_id == fix.action_id]
    assert attempt.final_at.at == "shape"

    # Rows a window holds of its own are never overwritten.
    minted = old.model_copy(deep=True)
    _window(minted, fix.action_id).problem_row_ids = [_eid("gone")]
    upgraded = compute_case_outcome(minted, case, at=WritePoint.COMMIT)
    assert _window(upgraded, fix.action_id).problem_row_ids == [_eid("gone")]

    # Carried as final once re-rooted, where a fresh read no longer fails.
    _deepen(case, hyp, root)
    again = compute_case_outcome(old, case, at=WritePoint.COMMIT)
    assert _window(again, fix.action_id).state == FixWindowState.FAILED
    assert "attempts.window:" + fix.action_id in again.disagreements


# ---------------------------------------------------------------------------
# Counting (#2689): once, at the helper
# ---------------------------------------------------------------------------


async def test_a_disagreement_first_stored_by_a_patch_is_counted_once(counted, tenant):
    case, _root, _hyp = _identified_case()
    action = _fix(case, turn=3)
    record_case_outcome(case, at=WritePoint.CYCLE)
    action.accepted_in_turn = 2
    tenant(case)
    repository = await _stored(case)
    service = CaseService(repository)

    await service.update_case(case.case_id, {"description": "checkout 503s"})
    assert counted == ["attempts.accepted_in_turn"]

    await service.update_case(case.case_id, {"description": "checkout 503s again"})
    assert counted == ["attempts.accepted_in_turn"], "carried already: none again"


def test_each_shape_step_runs_once_and_only_on_its_own_shape(monkeypatch):
    """A record at the current shape, moved to the next one by a step that
    adds nothing: that step runs once, on a record of the shape below it, an
    off-cycle FAILED window the record holds non-final stays non-final (only
    the 1 -> 2 step marks what shape 1 held final), and the shape rises."""
    case, _root, _hyp, fix = _failed_after_a_cycle()
    record_before_save(case, at=WritePoint.COMMIT)
    stored = case.progress.outcome
    assert stored.shape == CURRENT_OUTCOME_SHAPE, "premise: written current"
    assert _window(stored, fix.action_id).final_at is None, "premise: off-cycle"
    following = CURRENT_OUTCOME_SHAPE + 1
    ran: list[int] = []

    def step(prior, *_):
        ran.append(prior.shape)
        return prior

    monkeypatch.setattr(outcome_module, "CURRENT_OUTCOME_SHAPE", following)
    monkeypatch.setitem(outcome_module.SHAPE_STEPS, following, step)

    folded = compute_case_outcome(stored, case, at=WritePoint.COMMIT)

    assert ran == [CURRENT_OUTCOME_SHAPE]
    assert folded.shape == following
    assert _window(folded, fix.action_id).final_at is None


# ---------------------------------------------------------------------------
# The metadata write bumps no version
# ---------------------------------------------------------------------------


async def test_a_description_edit_leaves_an_in_flight_turn_to_commit(tenant):
    """The review's probe: a turn loads the case at v1, the description is
    edited meanwhile, and the turn still commits (no 409)."""
    case = inquiry_case(user_id="u", enterprise_id="o", statement="it is slow")
    record_case_outcome(case, at=WritePoint.CYCLE)
    tenant(case)
    repository = await _stored(case)
    turn = await repository.get(case.case_id)

    await CaseService(repository).update_case(
        case.case_id, {"description": "checkout is slow"}
    )
    edited = await repository.get(case.case_id)
    assert edited.version == turn.version, "the scoped write bumps nothing"
    assert edited.progress.outcome.problem.statement == "checkout is slow"

    await commit_turn_plan(repository, turn, TurnCommitPlan())


class _Spied(_Copies):
    """Records each scoped metadata write; ``before_write`` runs once,
    ahead of the next one (a concurrent request landing first)."""

    def __init__(self):
        super().__init__()
        self.writes: list = []
        self.before_write = None

    async def update_metadata_fields(self, case_id, **fields):
        if self.before_write is not None:
            first, self.before_write = self.before_write, None
            await first()
        self.writes.append(fields)
        return await super().update_metadata_fields(case_id, **fields)


async def _spied(case) -> _Spied:
    repository = _Spied()
    await repository.save(case)
    return repository


async def test_an_unchanged_description_folds_nothing(tenant):
    """The title alone is written: no description, no record, no version."""
    case = inquiry_case(user_id="u", enterprise_id="o", statement="it is slow")
    record_case_outcome(case, at=WritePoint.CYCLE)
    tenant(case)
    repository = await _spied(case)
    before = (await repository.get(case.case_id)).progress.outcome

    await CaseService(repository).update_case(
        case.case_id, {"title": "Slow checkout", "description": case.description}
    )

    assert repository.writes == [{"title": "Slow checkout"}]
    saved = await repository.get(case.case_id)
    assert saved.title == "Slow checkout"
    assert saved.progress.outcome == before


async def test_an_unchanged_description_with_no_title_writes_nothing(tenant):
    case = inquiry_case(user_id="u", enterprise_id="o", statement="it is slow")
    record_case_outcome(case, at=WritePoint.CYCLE)
    tenant(case)
    repository = await _spied(case)

    await CaseService(repository).update_case(
        case.case_id, {"description": case.description}
    )

    assert repository.writes == []


async def test_a_concurrent_edit_keeps_the_description_beside_its_record(tenant):
    """The review's interleaving: request D2 (a title, and the description
    unchanged) loads first; request D1 (a new description) commits it with
    its record; then D2 writes. The stored description and the record's
    statement come from one write."""
    case = inquiry_case(user_id="u", enterprise_id="o", statement="API is slow")
    case.description = "API is slow"
    record_case_outcome(case, at=WritePoint.CYCLE)
    tenant(case)
    repository = await _spied(case)
    service = CaseService(repository)

    async def d1():
        await service.update_case(case.case_id, {"description": "checkout is slow"})

    repository.before_write = d1
    await service.update_case(
        case.case_id, {"title": "Slow API", "description": "API is slow"}
    )

    final = await repository.get(case.case_id)
    assert final.title == "Slow API"
    assert final.description == "checkout is slow"
    assert final.progress.outcome.problem.statement == "checkout is slow"
    assert narration_outcome(final) == final.progress.outcome


# ---------------------------------------------------------------------------
# Only a minting fold moves a record to terminal
# ---------------------------------------------------------------------------


async def test_a_close_whose_record_write_raised_is_left_for_the_repair(
    tenant, monkeypatch
):
    """The review's probe: the closed write raises inside ``close_case``;
    the write helper's fold carries the open ``folded_under``, so
    ``needs_record`` holds and the backfill repairs it."""
    from unittest.mock import patch

    from faultmaven.cli.backfill_case_outcome import (
        backfill_case_outcome,
        needs_record,
    )

    case, _root, _hyp = _identified_case()
    record_case_outcome(case, at=WritePoint.CYCLE)
    tenant(case)
    repository = await _stored(case)
    real = outcome_module.compute_case_outcome

    def _closed_raises(prior, case, *, at, closure=None):
        if at == WritePoint.CLOSED:
            raise RuntimeError("the closed write")
        return real(prior, case, at=at, closure=closure)

    with monkeypatch.context() as patched:
        patched.setattr(outcome_module, "compute_case_outcome", _closed_raises)
        await CaseService(repository).close_case(case.case_id, case.effective_driver_id)
    saved = await repository.get(case.case_id)
    assert saved.state == CaseState.CLOSED, "premise: closed"
    assert saved.progress.outcome.folded_under == CaseState.INVESTIGATING
    assert needs_record(saved)

    with patch(
        "faultmaven.providers.tenancy.factory.requested_tenant_provider",
        return_value="single",
    ):
        assert (
            await backfill_case_outcome(
                enterprise_id=None, dry_run=False, repository=repository
            )
            == 0
        )
    repaired = (await repository.get(case.case_id)).progress.outcome
    assert repaired.folded_under == CaseState.CLOSED
    assert repaired.closure.reason == saved.closure_reason


# ---------------------------------------------------------------------------
# The reclassification re-dates the rows the record reads
# ---------------------------------------------------------------------------


async def test_a_reclassified_relief_row_saves_a_matching_record(tenant):
    """A mitigated case relieved at 7 on two rows: the first dated by the
    instant it inherited from its file, so it is the relief row. Its file is
    reclassified and the new extractor parses no timestamps, so the row's
    instant goes with the file's (#1471): the saved record names the other
    relief row, as the saved case reads it."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.models.api import DataType
    from faultmaven.modules.agent.domain.services.investigation_service.reclassification import (  # noqa: E501
        EvidenceReclassifier,
    )
    from tests.unit.core.investigation.bound_rows import bound_row
    from tests.unit.core.investigation.golden_cases_2636 import _accepted_mitigation
    from tests.unit.modules.agent.conftest import make_preprocessing_result

    case, shown = _accepted_mitigation()
    instant = datetime.now(timezone.utc) - timedelta(minutes=5)
    dated = {
        "coverage_start_ts": instant,
        "coverage_end_ts": instant,
        "coverage_source": "iso8601",
    }
    first = bound_row(
        case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=7, rechecks=shown
    )
    (upload,) = [f for f in case.uploaded_files if f.file_id == first.source_file_id]
    upload.storage_ref = "evidence/relief.log"
    for row in (upload, first):
        for field, value in dated.items():
            setattr(row, field, value)
    second = bound_row(
        case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=7, rechecks=shown
    )
    case.current_turn = 7
    record_case_outcome(case, at=WritePoint.CYCLE)
    assert case.progress.outcome.mitigation.relief_row_id == first.evidence_id
    tenant(case)
    repository = await _stored(case)
    preprocessing = MagicMock()
    preprocessing.reclassify_evidence = AsyncMock(
        return_value=make_preprocessing_result()
    )
    storage = MagicMock()
    storage.retrieve_file = AsyncMock(return_value=b"checkout ok\n")

    await EvidenceReclassifier(
        case_repository=repository,
        file_storage_service=storage,
        preprocessing_service=preprocessing,
    ).reclassify_out_of_band(
        case.case_id, first.evidence_id, case.user_id, DataType.LOGS_AND_ERRORS
    )

    saved = await repository.get(case.case_id)
    (row,) = [e for e in saved.evidence if e.evidence_id == first.evidence_id]
    assert row.coverage_end_ts is None, "premise: the instant went with the file"
    mitigation = saved.progress.outcome.mitigation
    assert mitigation.relief_row_id == second.evidence_id
    assert mitigation.span_seconds is None
    assert _matches(saved)


# ---------------------------------------------------------------------------
# A failed save leaves the case as it was
# ---------------------------------------------------------------------------


class _Refuses(_Copies):
    async def save(self, case, **rows):
        if case.is_persisted:
            raise RuntimeError("the commit failed")
        return await super().save(case, **rows)


async def test_a_failed_commit_restores_the_offers_and_the_record():
    _engine_, case, _result = await _offered_then_declined()
    repository = _Refuses()
    await repository.save(case)
    case.evidence.append(_user_confirmation(case.current_turn))
    offers, outcome = case.offer_record, case.progress.outcome
    assert lapse_offers(case) != offers, "premise: the lapse moves the record"

    with pytest.raises(RuntimeError):
        await commit_turn_plan(repository, case, TurnCommitPlan())

    assert case.offer_record is offers
    assert case.progress.outcome is outcome


async def test_a_failed_write_restores_the_offers_and_the_record(tenant):
    from faultmaven.modules.case.utils import update_case_with_retry

    _engine_, case, _result = await _offered_then_declined()
    tenant(case)
    repository = _Refuses()
    await repository.save(case)
    seen = []

    async def mutate(loaded):
        loaded.evidence.append(_user_confirmation(loaded.current_turn))
        seen.append((loaded, loaded.offer_record, loaded.progress.outcome))

    with pytest.raises(RuntimeError):
        await update_case_with_retry(repository, case.case_id, mutate)

    ((loaded, offers, outcome),) = seen
    assert loaded.offer_record is offers
    assert loaded.progress.outcome is outcome


# ---------------------------------------------------------------------------
# Creation and the write points
# ---------------------------------------------------------------------------


async def test_a_new_case_is_saved_with_a_first_record():
    repository = InMemoryCaseRepository()
    case = await CaseService(repository).create_case(
        title="Slow checkout", description="checkout is slow", owner_id="u"
    )
    record = (await repository.get(case.case_id)).progress.outcome
    assert record.problem.statement == "checkout is slow"
    assert record.attempts == [] and record.folded_under == CaseState.INQUIRY


def test_a_misspelt_write_point_is_refused_and_counted(monkeypatch):
    from types import SimpleNamespace

    case, _root, _hyp = _identified_case()
    failed: list = []
    monkeypatch.setattr(
        outcome_module,
        "case_outcome_write_failures_total",
        SimpleNamespace(
            labels=lambda at: SimpleNamespace(inc=lambda: failed.append(at))
        ),
    )
    assert record_case_outcome(case, at="cylce") is False
    assert failed == ["cylce"]
    with pytest.raises(ValueError):
        compute_case_outcome(None, case, at="cylce")
