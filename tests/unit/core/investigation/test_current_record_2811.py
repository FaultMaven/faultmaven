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
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.case_builders import inquiry_case
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
    record_case_outcome(case, at="cycle")
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
    record_case_outcome(case, at="cycle")
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
    record_case_outcome(case, at="cycle")
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
    record_case_outcome(case, at="cycle")
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
    unlapsed = compute_case_outcome(case.progress.outcome, case, at="commit")
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
    record_case_outcome(case, at="cycle")
    assert _window(case.progress.outcome, fix.action_id).final_at is None
    _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    return case, root, hyp, fix


def test_a_failed_window_first_read_at_a_commit_is_not_final():
    case, root, hyp, fix = _failed_after_a_cycle()

    record_before_save(case, at="commit")
    window = _window(case.progress.outcome, fix.action_id)
    assert window.state == FixWindowState.FAILED
    assert window.final_at is None, "a commit mints no final verdict"

    # The next cycle re-reads it: re-rooted, the window no longer fails, and
    # nothing is carried or counted as a disagreement.
    _deepen(case, hyp, root)
    record_case_outcome(case, at="cycle")
    record = case.progress.outcome
    assert _window(record, fix.action_id).state != FixWindowState.FAILED
    assert record.disagreements == {}


def test_the_next_cycle_mints_a_failure_it_still_reads():
    case, _root, _hyp, fix = _failed_after_a_cycle()
    record_before_save(case, at="commit")
    record_case_outcome(case, at="cycle")
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
    record_case_outcome(case, at="closed", closure=decided)
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
    old = _old_shape(compute_case_outcome(None, case))
    assert old.shape == 1
    fresh_rows = _window(compute_case_outcome(None, case), fix.action_id)
    assert fresh_rows.problem_row_ids == [_eid("relief")], "premise"

    record = compute_case_outcome(old, case, at="commit")

    window = _window(record, fix.action_id)
    assert record.shape == CURRENT_OUTCOME_SHAPE
    assert window.state == FixWindowState.FAILED
    assert window.observed is True
    assert window.final_at.at == "shape"
    assert window.problem_row_ids == [_eid("relief")]
    (attempt,) = [a for a in record.attempts if a.action_id == fix.action_id]
    assert attempt.final_at.at == "shape"

    # Carried as final once re-rooted, where a fresh read no longer fails.
    _deepen(case, hyp, root)
    again = compute_case_outcome(old, case, at="commit")
    assert _window(again, fix.action_id).state == FixWindowState.FAILED
    assert "attempts.window:" + fix.action_id in again.disagreements


# ---------------------------------------------------------------------------
# Counting (#2689): once, at the helper
# ---------------------------------------------------------------------------


async def test_a_disagreement_first_stored_by_a_patch_is_counted_once(counted, tenant):
    case, _root, _hyp = _identified_case()
    action = _fix(case, turn=3)
    record_case_outcome(case, at="cycle")
    action.accepted_in_turn = 2
    tenant(case)
    repository = await _stored(case)
    service = CaseService(repository)

    await service.update_case(case.case_id, {"description": "checkout 503s"})
    assert counted == ["attempts.accepted_in_turn"]

    await service.update_case(case.case_id, {"description": "checkout 503s again"})
    assert counted == ["attempts.accepted_in_turn"], "carried already: none again"
