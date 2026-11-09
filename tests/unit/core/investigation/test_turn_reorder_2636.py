"""The turn reorder (#2636, #2615 stage R): outcome text after the record.

Within a decision cycle no text that states an outcome is composed before the
last decider has run and ``progress.outcome`` is written. Pin 4 drives a turn
whose step 1 refuses and whose step 4 retires through ``process_turn``; the
regressions for #2603 and #2555 fail on ``main``. No live LLM: the generator is
stubbed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import turn_application
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    record_decision,
)
from faultmaven.core.investigation.narration.codes import NoticeCode
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.core.investigation.problem_status import (
    decline_revision,
    propose_revision,
)
from faultmaven.core.investigation.progress_monitor import (
    ProgressCheckResult,
    ProgressMonitor,
)
from faultmaven.core.investigation.schemas import (
    EvidenceTrail,
    HypothesisUpdate,
    InvestigationResponse_Diagnosis,
    MilestoneJustifications,
    MilestoneUpdates,
)
from faultmaven.modules.case.contracts import (
    AnswerVia,
    CauseState,
    EvidenceCategory,
    HypothesisState,
    ProblemStatus,
    TurnOutcome,
)
from tests.unit.core.investigation.bound_rows import _row
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_case_outcome_fold_2636 import _fix
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _eid,
    _identified_case,
)
from tests.unit.core.investigation.test_mitigation_record_2100 import (
    _accepted,
    _feedback,
    _pending,
    _turn,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _DSU,
    _apply,
    _engine,
    _standing,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import _make_repo

pytestmark = pytest.mark.unit

_PROCEEDED = (
    "SYSTEM: mitigation_verified was not recorded — the mitigation is recorded "
    "as proceed_unmitigated, which is written once."
)


# ---------------------------------------------------------------------------
# Pin 4: the turn order, through process_turn
# ---------------------------------------------------------------------------


def _deadlock(case):
    """The progress monitor's deadlock repair, as step 4 runs it."""
    repair = ProgressMonitor()._repair_deadlock(case)
    return ProgressCheckResult(
        transparent_mode=True,
        repair_type=repair.repair_type,
        repair_action=repair.action,
        repair_message=repair.message,
        prompt_injection=repair.prompt_injection,
    )


async def test_pin_4_a_turn_that_refuses_then_retires_renders_one_text(monkeypatch):
    """Step 1 refuses the mitigation's verify, records the same response's
    ``proceed_unmitigated`` and refuses an unfounded refutation; step 4's
    deadlock repair retires the hypothesis that refutation named. Every
    outcome-bearing line states where the cycle ended: no re-check is asked
    of a proceeded mitigation (#2603), and a retired hypothesis is not said
    to stay open (A19)."""
    case, _shown = _pending()
    h_id = _standing(case)
    case.hypotheses[h_id].state = HypothesisState.INCONCLUSIVE
    case.current_turn = 6
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    monkeypatch.setattr(engine.deps.progress_monitor, "check_progress", _deadlock)
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.",
            evidence_trail=EvidenceTrail(
                evidence_analyzed=[],
                milestone_justifications=MilestoneJustifications(
                    mitigation_accepted="the user moved orders to the replica",
                    mitigation_verified="the user says orders go through",
                    mitigation_disposition="the user goes on without a re-check",
                ),
            ),
            state_updates=_DSU(
                milestones=MilestoneUpdates(
                    mitigation_accepted=True,
                    mitigation_verified=True,
                    mitigation_disposition="proceed_unmitigated",
                ),
                hypotheses_to_update=[
                    HypothesisUpdate(
                        hypothesis_id=h_id,
                        state=HypothesisState.REFUTED,
                        refutation_reason="it happens on every node",
                    )
                ],
            ),
        )
    )

    await engine.process_turn(
        case=case, user_message="the replica is serving, moving on", redaction_ctx=None
    )

    assert case.progress.mitigation.disposition == "proceed_unmitigated"
    assert case.hypotheses[h_id].state == HypothesisState.RETIRED
    notices = case.turn_history[-1].system_notices
    feedback = "\n".join(notices)
    assert _PROCEEDED in notices
    assert "record the post-mitigation" not in feedback
    refused = [n for n in notices if n.startswith(f"Hypothesis {h_id} was NOT")]
    assert len(refused) == 1 and "It stays open." not in refused[0]
    assert "⁣" not in feedback, "every token was rendered"
    assert case.progress.outcome.mitigation.disposition == "proceed_unmitigated"


# ---------------------------------------------------------------------------
# #2603: a refused verify beside a same-response proceed asks no re-check
# ---------------------------------------------------------------------------


async def test_2603_a_refused_verify_beside_a_proceed_asks_for_no_recheck():
    eng, case, _shown = await _accepted()
    meta = await _turn(
        eng,
        case,
        7,
        mitigation_verified=True,
        mitigation_disposition="proceed_unmitigated",
    )
    record = case.progress.mitigation
    assert (record.verified, record.disposition) == (False, "proceed_unmitigated")
    assert _PROCEEDED in meta["system_notices"]
    assert "record the post-mitigation" not in _feedback(meta)


# ---------------------------------------------------------------------------
# #2555: a re-check recorded during a hold is narrated once the hold ends
# ---------------------------------------------------------------------------


def _held_recheck():
    """A fix run at 6; a revision held at 7, on whose turn the user's re-check
    of the problem lands naming nothing it re-checks."""
    eng = _engine()
    case, _root, _hyp = _identified_case()
    _fix(case)
    case.current_turn = 7
    propose_revision(
        case,
        text="checkout returns 503 only under peak load",
        evidence_ids=[case.evidence[0].evidence_id],
        basis="the 503s come back only at peak",
        wording_key="revision:rev_2555",
    )
    assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
    _row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id=_eid("held"),
        summary="checkout answers 200",
    )
    return eng, case


def _unbound_line(meta) -> list[str]:
    return [n for n in meta.get("system_notices") or [] if _eid("held") in n]


async def test_2555_a_recheck_held_through_a_revision_is_narrated_once_it_ends():
    eng, case = _held_recheck()
    held = await _apply(eng, case, _DSU())
    assert not _unbound_line(held), "nothing binds while the problem is held"

    case.current_turn = 8
    decline_revision(case, via=AnswerVia.CLICK)
    assert case.progress.problem_status == ProblemStatus.VERIFIED
    live = await _apply(eng, case, _DSU())
    (line,) = _unbound_line(live)
    assert "name nothing they re-check that binds" in line

    case.current_turn = 9
    assert not _unbound_line(await _apply(eng, case, _DSU())), "narrated once"
    (entry,) = case.progress.outcome.unbound_rechecks
    assert entry.narrated_turn == 8


async def test_the_watermark_narrates_no_row_a_record_before_it_holds():
    """A stored case whose record PR A wrote: its unbound row predates the
    first narration, so it is treated as narrated, not narrated on the first
    turn after the deploy."""
    eng, case = _held_recheck()
    decline_revision(case, via=AnswerVia.CLICK)
    case.progress.outcome = compute_case_outcome(None, case)
    assert case.progress.outcome.narration_since is None
    assert case.progress.outcome.unbound_rechecks
    case.current_turn = 9
    meta = await _apply(eng, case, _DSU())
    assert not _unbound_line(meta)
    assert case.progress.outcome.narration_since == 9


# ---------------------------------------------------------------------------
# The decision log and the final recompute
# ---------------------------------------------------------------------------


def test_a_decision_recorded_twice_holds_one_place():
    metadata: dict = {}
    for _ in range(2):
        record_decision(metadata, NoticeCode.FIX_UNTARGETED, action_id="act_1")
        record_decision(metadata, NoticeCode.SOLUTION_OFFER_WITHDRAWN, count=1)
    assert len(metadata["system_notices"]) == 2
    (withdrawn,) = [
        e
        for e in metadata["outcome_decisions"]
        if e["code"] == NoticeCode.SOLUTION_OFFER_WITHDRAWN
    ]
    assert withdrawn["params"]["count"] == 2, "the counts aggregate (A9)"


def test_the_final_recompute_drops_a_fallen_identification(monkeypatch):
    """The falling edge (A12): an identification this turn's step 1 recorded
    and the final recompute dropped leaves the turn's milestones, and the
    turn's outcome is re-derived; ``progress_made`` keeps step 1's reading."""
    case, _root, _hyp = _identified_case()
    monkeypatch.setattr(
        turn_application, "_recompute_assessment_state", lambda *a, **k: None
    )
    case.progress.cause_state = CauseState.CANDIDATES
    metadata = {
        "milestones_completed": ["root_cause_identified"],
        "progress_made": True,
        "outcome": TurnOutcome.MILESTONE_COMPLETED,
        turn_application.ASSESSMENT_RECOMPUTED_KEY: "stub",
    }
    turn_application._final_recompute(case, metadata, TurnOutcome.CONVERSATION)
    assert metadata["milestones_completed"] == []
    assert metadata["outcome"] != TurnOutcome.MILESTONE_COMPLETED
    assert metadata["progress_made"] is True


async def test_the_final_recompute_does_not_double_a_step_one_notice():
    """Two recomputes in one cycle flag the same unbound row into one notice."""
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _flag_unbound_post_fix_rows,
    )

    _eng, case = _held_recheck()
    decline_revision(case, via=AnswerVia.CLICK)
    metadata: dict = {}
    _flag_unbound_post_fix_rows(case, metadata)
    _flag_unbound_post_fix_rows(case, metadata)
    assert len(narrated(case, metadata)) == 1
