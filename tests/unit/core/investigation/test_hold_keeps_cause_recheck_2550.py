"""A cause re-check recorded while a statement revision waits (#2550).

A causal_absence row recorded before the problem is verified is recorded as
symptom_absence: a cause cannot be shown eliminated before its problem is
verified. A revision over a VERIFIED problem holds a problem shown real, so its
hold turns' rows stay causal_absence, and the confirmation's replayed cause work
binds a re-check of the staged cause. A hold over a problem never verified (the
verifying turn's own revision, a false alarm's) still reclassifies.

Driven through ``_apply_investigation_updates`` and the user's answer
(``confirm_revision`` / ``decline_revision``). No live LLM.
"""

import pytest

from faultmaven.core.investigation.cause_assurance import (
    SUBJECT_CAUSE,
    cause_recheck_status,
    fix_windows,
    subject_index,
)
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
)
from faultmaven.core.investigation.problem_status import (
    decline_revision,
    invalidate_problem,
    pending_revision,
    problem_shown_real,
)
from faultmaven.modules.case.contracts import AnswerVia, EvidenceCategory, ProblemStatus
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)
from tests.unit.core.investigation.bound_rows import bound_to_add

pytestmark = pytest.mark.unit

CAUSAL_ABSENCE = EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
SYMPTOM_ABSENCE = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
SYMPTOM = EvidenceCategory.SYMPTOM_EVIDENCE


def _recheck(case, *, turn: int, of: str):
    """The model's re-check of the cause row ``of`` (a ``new_index_N`` ref),
    on data uploaded at ``turn`` (``bound_rows.upload``): the cause is gone."""
    summary = "the orders pool now reads 50 connections"
    return bound_to_add(case, CAUSAL_ABSENCE, summary, turn=turn, rechecks=of)


async def _revision_turn_with_recheck(engine, case) -> str:
    """Turn 4: the revision, the cause work and a re-check of a staged cause
    row, on a file uploaded that turn. Returns the re-check's id."""
    dsu = revision._revision_turn_with_cause_work()
    dsu.evidence_to_add.append(_recheck(case, turn=4, of="new_index_1"))
    meta = await revision._apply(engine, case, dsu)
    return meta["evidence_added"][-1]


def _row(case, evidence_id: str):
    return next(e for e in case.evidence if e.evidence_id == evidence_id)


def _assert_counts_for_the_cause(case, evidence_id: str) -> None:
    assert _row(case, evidence_id).category == CAUSAL_ABSENCE
    subjects = subject_index(case)
    assert subjects.subject(_row(case, evidence_id), windows=True) == SUBJECT_CAUSE
    assert [(e.evidence_id, why) for e, why in cause_recheck_status(case)] == [
        (evidence_id, None)
    ]
    assert [e.evidence_id for e in fix_windows(case)[0].cause_rows] == [evidence_id]


class TestAHoldOverAVerifiedProblem:
    async def test_a_recheck_on_the_proposal_turn_counts_once_confirmed(self):
        engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
        absent = await _revision_turn_with_recheck(engine, case)
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING

        case.current_turn = 5
        await confirm_revision(engine.responses, case, {}, via=AnswerVia.CLICK)

        assert case.progress.problem_status == ProblemStatus.VERIFIED
        _assert_counts_for_the_cause(case, absent)

    async def test_a_recheck_on_a_later_hold_turn_counts_once_confirmed(self):
        engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
        await revision._apply(
            engine,
            case,
            revision._DSU(
                evidence_to_add=[revision._row(SYMPTOM, "s1")],
                verification_updates=revision._revision_update("new_index_0"),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING

        # Hold turn 5 brings the cause work and its re-check. Without the
        # revision's symptom row, the cause rows are new_index_0 and _1.
        case.current_turn = 5
        dsu = revision._revision_turn_with_cause_work()
        dsu.verification_updates = None
        del dsu.evidence_to_add[0]
        shifted = {"new_index_1": "new_index_0", "new_index_2": "new_index_1"}
        for link in dsu.node_evidence_links:
            link.evidence_id_ref = shifted[link.evidence_id_ref]
        dsu.hypotheses_to_add[0].terminal_basis_evidence_ids = ["new_index_1"]
        dsu.evidence_to_add.append(_recheck(case, turn=5, of="new_index_0"))
        meta = await revision._apply(engine, case, dsu)
        absent = meta["evidence_added"][-1]

        case.current_turn = 6
        await confirm_revision(engine.responses, case, {}, via=AnswerVia.CLICK)

        assert case.progress.problem_status == ProblemStatus.VERIFIED
        _assert_counts_for_the_cause(case, absent)

    async def test_a_declined_revision_leaves_no_symptom_absence(self):
        engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
        absent = await _revision_turn_with_recheck(engine, case)

        case.current_turn = 5
        decline_revision(case, via=AnswerVia.CLICK)

        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert _row(case, absent).category == CAUSAL_ABSENCE
        assert SYMPTOM_ABSENCE not in {e.category for e in case.evidence}


class TestAHoldOverAProblemNeverVerified:
    async def test_a_revision_over_a_false_alarm_reclassifies(self):
        engine, case = revision._engine(), revision._case()
        finding = revision._with(case, revision._evidence(SYMPTOM_ABSENCE, "a"))
        invalidate_problem(case, evidence_ids=finding, basis="the alert misfired")
        await revision._apply(
            engine,
            case,
            revision._DSU(
                evidence_to_add=[
                    revision._row(SYMPTOM, "s1"),
                    revision._row(CAUSAL_ABSENCE, "g"),
                ],
                verification_updates=revision._revision_update("new_index_0"),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
        assert pending_revision(case).prior_status == ProblemStatus.INVALIDATED
        assert case.evidence[-1].category == SYMPTOM_ABSENCE


@pytest.mark.parametrize("superseded", [False, True])
@pytest.mark.parametrize(
    "prior, shown",
    [
        (ProblemStatus.VERIFIED, True),
        (ProblemStatus.UNVERIFIED, False),
        (ProblemStatus.INVALIDATED, False),
    ],
)
def test_a_hold_shows_the_problem_real_only_over_a_verified_one(
    prior, shown, superseded
):
    case = revision._case(prior)
    assert problem_shown_real(case) is shown
    revision._propose(case)
    if superseded:
        revision._propose(
            case,
            "Checkout requests to /orders time out after 30s at the gateway",
            evidence=list(pending_revision(case).evidence_ids),
        )
    assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
    assert pending_revision(case).prior_status == prior
    assert problem_shown_real(case) is shown
