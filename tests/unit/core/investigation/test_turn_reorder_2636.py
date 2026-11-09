"""The turn reorder (#2636, #2615 stage R): outcome text after the record.

Within a decision cycle no text that states an outcome is composed before the
last decider has run and ``progress.outcome`` is written. Pin 4 drives a turn
whose step 1 refuses and whose step 4 retires through ``process_turn``; the
regressions for #2603 and #2555 fail on ``main``. No live LLM: the generator is
stubbed.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import turn_application
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    record_decision,
)
from faultmaven.core.investigation.milestone_engine.terminal_proposals import (
    _maybe_propose_confirmed_resolution,
    _maybe_propose_deferred_close,
)
from faultmaven.core.investigation.milestone_engine.turn_narration import narrate_turn
from faultmaven.core.investigation.narration.codes import NoticeCode, OfferCode
from faultmaven.core.investigation.narration.notices import render_notice
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
    CaseOutcome,
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
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
    _make_repo,
)

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


def _stored(case) -> None:
    """The record PR A stored for ``case``: as loaded, with no narration."""
    record = compute_case_outcome(None, case)
    case.progress.outcome = CaseOutcome.model_validate(record.model_dump())


async def test_the_watermark_marks_the_stored_rows_and_narrates_every_later_one():
    """Review item 5. The rows a stored record held before the first narration
    are marked narrated, unrendered; a row that first appears after it is
    narrated once, though its data predates the watermark (#2555)."""
    eng, case = _held_recheck()
    decline_revision(case, via=AnswerVia.CLICK)
    _stored(case)
    case.current_turn = 9
    first = await _apply(eng, case, _DSU())
    assert not _unbound_line(first), "the stored row is not narrated"
    (stored,) = case.progress.outcome.unbound_rechecks
    assert stored.narrated_turn == 9 and case.progress.outcome.narration_since == 9

    late = _row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=8,
        evidence_id=_eid("late"),
        summary="checkout answers 200 again",
    )
    late.collected_at_turn = 10
    case.current_turn = 10
    later = await _apply(eng, case, _DSU())
    lines = [n for n in later["system_notices"] if _eid("late") in n]
    assert len(lines) == 1 and _eid("held") not in lines[0]


async def test_a_row_the_notice_did_not_name_is_narrated_on_a_later_turn():
    """Review item 6: the notice names five rows a line; only those are
    marked narrated, and the other three are named on the next turn."""
    eng, case = _held_recheck()
    for n in range(7):
        _row(
            case,
            EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            turn=7,
            evidence_id=_eid(f"more{n}"),
            summary=f"checkout answers 200 ({n})",
        )
    await _apply(eng, case, _DSU())
    case.current_turn = 8
    decline_revision(case, via=AnswerVia.CLICK)
    first = await _apply(eng, case, _DSU())
    rows = case.progress.outcome.unbound_rechecks
    named = {u.evidence_id for u in rows if u.narrated_turn == 8}
    assert len(rows) == 8 and len(named) == 5
    assert all(e in "\n".join(first["system_notices"]) for e in named)
    case.current_turn = 9
    second = "\n".join((await _apply(eng, case, _DSU()))["system_notices"])
    rest = {u.evidence_id for u in rows} - named
    assert len(rest) == 3 and all(e in second for e in rest)
    assert all(
        u.narrated_turn is not None for u in case.progress.outcome.unbound_rechecks
    )


# ---------------------------------------------------------------------------
# No record written: an outcome in memory, never an empty block
# ---------------------------------------------------------------------------


def _backstop(monkeypatch=None):
    case = _confirmed_case()
    case.current_turn = 2
    metadata: dict = {}
    _maybe_propose_confirmed_resolution(case, metadata)
    return case, metadata


def test_with_no_record_the_offer_renders_from_an_outcome_in_memory():
    """Review item 3: the cycle's write failed (no ``OUTCOME_WRITTEN_KEY``);
    the narration computes the outcome in memory and stores nothing."""
    case, metadata = _backstop()
    narrate_turn(case, metadata)
    text = metadata["resolution_ready_gate_message"]
    assert text.startswith("The record shows:\n\n- **The cause**")
    assert case.progress.outcome is None, "nothing stored"


def test_with_no_outcome_at_all_no_empty_record_block_renders(monkeypatch):
    from faultmaven.core.investigation.milestone_engine import turn_narration

    def _boom(*_a, **_k):
        raise RuntimeError("no outcome")

    monkeypatch.setattr(turn_narration, "compute_case_outcome", _boom)
    case, metadata = _backstop()
    narrate_turn(case, metadata)
    text = metadata["resolution_ready_gate_message"]
    assert "The record shows" not in text
    assert text.startswith("The case is still open until you confirm")


def test_with_no_record_a_proceeded_mitigation_asks_for_no_recheck():
    """Review item 4: the disposition comes from the log, and no turn clause
    names an absent turn."""
    params = {"reason": "no_bound_relief", "problem_row_ids": ["ev_p"]}
    disposed = {
        "code": NoticeCode.MITIGATION_DISPOSED,
        "params": {"value": "proceed_unmitigated"},
    }
    code = NoticeCode.MITIGATION_VERIFY_REFUSED
    assert render_notice(code, params, None, [disposed]) == [_PROCEEDED]
    (line,) = render_notice(code, params, None, [])
    assert "None" not in line
    assert "relieved after the mitigation: record" in line


# ---------------------------------------------------------------------------
# The proposal's summary renders the branch decided
# ---------------------------------------------------------------------------


def test_a_proposals_summary_renders_the_decided_branch_and_counts_a_conflict(
    monkeypatch,
):
    """Review item 7: the model's ``closed`` decided on a close summary; the
    record now reads the case resolvable. The summary is the decided branch,
    never re-derived, and the disagreement is counted."""
    from faultmaven.core.investigation.milestone_engine import turn_narration
    from faultmaven.core.investigation.narration.offers import render_offer

    counted = []
    monkeypatch.setattr(
        turn_narration,
        "case_outcome_conflicts_total",
        SimpleNamespace(
            labels=lambda field: SimpleNamespace(inc=lambda: counted.append(field))
        ),
    )
    case = _confirmed_case()
    record = compute_case_outcome(None, case)
    assert record.closure.eligibility == "suggest_resolve"
    params = {
        "composer": "closure",
        "verdict": "has_substance",
        "evidence_count": 2,
        "hypothesis_count": 0,
        "milestones_completed": 0,
        "root_cause": None,
        "root_cause_name": "the identified root cause",
        "identification_leg": None,
        "solution_titles": [],
    }
    text = render_offer(OfferCode.MODEL_PROPOSAL, params, record)
    assert text.startswith("Here's what was accomplished")
    turn_narration._conflict(OfferCode.MODEL_PROPOSAL, params, record)
    # The verdict, and the identified cause the decision did not name.
    assert counted == ["narration.model_proposal", "narration.model_proposal"]


# ---------------------------------------------------------------------------
# The decision log and the final recompute
# ---------------------------------------------------------------------------


def _responses():
    return SimpleNamespace(
        kb_prefetcher=SimpleNamespace(prefetch_kb_context=AsyncMock())
    )


def _cycle_metadata(**extra) -> dict:
    return {
        "milestones_completed": [],
        "hypotheses_validated": [],
        "progress_made": True,
        turn_application.ASSESSMENT_RECOMPUTED_KEY: "stub",
        **extra,
    }


def test_a_decision_recorded_twice_holds_one_place():
    metadata: dict = {}
    for _ in range(2):
        record_decision(metadata, NoticeCode.FIX_UNTARGETED, action_id="act_1")
        record_decision(metadata, NoticeCode.SOLUTION_OFFER_WITHDRAWN)
    assert len(metadata["system_notices"]) == 2


async def test_the_final_recompute_drops_a_fallen_identification():
    """The falling edge (A12), through the real recompute: step 1 identified
    the cause, and the hypothesis it rests on left the pool after it (as
    housekeeping retires one)."""
    case, _root, hyp = _identified_case()
    case.progress.cause_state = CauseState.CANDIDATES  # as the turn began
    step_one = _cycle_metadata()
    _recompute_assessment_state(case, metadata=step_one)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert step_one["milestones_completed"] == ["root_cause_identified"]
    step_one["outcome"] = TurnOutcome.MILESTONE_COMPLETED
    hyp.state = HypothesisState.RETIRED
    await turn_application._final_recompute(
        _responses(), case, step_one, TurnOutcome.CONVERSATION
    )
    assert case.progress.cause_state != CauseState.IDENTIFIED
    assert step_one["milestones_completed"] == []
    assert step_one["outcome"] != TurnOutcome.MILESTONE_COMPLETED
    assert step_one["progress_made"] is True


async def test_the_final_recompute_takes_a_rising_edge_as_step_one_does():
    """Review item 11: an identification the final recompute raises records
    the milestone, re-derives the outcome and runs step 1's KB warm-up."""
    case, _root, _hyp = _identified_case()
    case.progress.cause_state = CauseState.CANDIDATES
    metadata = _cycle_metadata(outcome=TurnOutcome.CONVERSATION)
    responses = _responses()
    await turn_application._final_recompute(
        responses, case, metadata, TurnOutcome.CONVERSATION
    )
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert metadata["milestones_completed"] == ["root_cause_identified"]
    assert metadata["outcome"] != TurnOutcome.CONVERSATION
    responses.kb_prefetcher.prefetch_kb_context.assert_awaited_once()
    assert responses.kb_prefetcher.prefetch_kb_context.await_args.args[2] == (
        "root_cause"
    )


async def test_the_final_recompute_does_not_double_a_step_one_notice():
    """Step 1's recompute and the final one flag the same unbound row into
    one notice."""
    _eng, case = _held_recheck()
    decline_revision(case, via=AnswerVia.CLICK)
    metadata = _cycle_metadata()
    _recompute_assessment_state(case, metadata=metadata)
    await turn_application._final_recompute(
        _responses(), case, metadata, TurnOutcome.CONVERSATION
    )
    assert len(narrated(case, metadata)) == 1


async def test_a_deferred_offer_whose_licence_decayed_lapses_unrendered():
    """Review item 8 (INV-32): the deferred close opened in step 1 on a
    working conclusion; housekeeping's decay takes it below the bar, the final
    recompute withdraws the licence, and the offer lapses without its "the
    root cause and fix are documented" prose."""
    case = _deferred_case()
    metadata = _cycle_metadata()
    _maybe_propose_deferred_close(case, metadata)
    assert case.offer_record.open is not None
    for hypothesis in case.hypotheses.values():
        hypothesis.likelihood = 0.2  # housekeeping's decay
    await turn_application._final_recompute(
        _responses(), case, metadata, TurnOutcome.CONVERSATION
    )
    assert case.offer_record.open is None
    (answer,) = case.offer_record.answers
    assert (answer.answer, answer.via) == ("lapsed", "engine")
    narrated(case, metadata)
    assert "deferred_solution_gate_message" not in metadata
    codes = [e["code"] for e in metadata["outcome_decisions"]]
    assert NoticeCode.OFFER_LAPSED in codes


def _deferred_case():
    """Deferred fix, licensed only by a working conclusion at 0.8."""
    from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _refresh_working_conclusion,
        _solution_cause_validated,
    )
    from faultmaven.modules.case.contracts import HypothesisCategory, SolutionFeasible
    from tests.unit.core.investigation.test_surgical_strip_and_cause_state import (
        TestDeferredImplementationClose,
        _solution,
    )

    case = TestDeferredImplementationClose()._case(
        feasible=SolutionFeasible.DEFERRED,
        solution_proposed=True,
        cause_identified=False,
    )
    case.solutions = [_solution()]
    case.progress.problem_status = ProblemStatus.VERIFIED
    h = HypothesisManager().create_hypothesis(
        statement="the role's trust policy names the wrong OIDC provider",
        category=HypothesisCategory.CONFIG,
        initial_likelihood=0.8,
        current_turn=1,
    )
    h.likelihood = 0.8
    case.hypotheses[h.hypothesis_id] = h
    _refresh_working_conclusion(case)
    assert _solution_cause_validated(case), "premise: licensed"
    return case


# ---------------------------------------------------------------------------
# The two order changes the plan's order makes (review items 9 and 10)
# ---------------------------------------------------------------------------


async def test_the_pending_check_notice_follows_the_monitors(monkeypatch):
    """An expected difference: the backstop now runs after the progress
    monitor, so its pending-check notice follows the monitor's injection."""
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _engine as _held_engine,
    )
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _held_case,
    )
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _turn as _held_turn,
    )

    engine, case = _held_engine(), _held_case()
    monkeypatch.setattr(
        engine.deps.progress_monitor,
        "check_progress",
        lambda _case: ProgressCheckResult(
            transparent_mode=True, prompt_injection="MONITOR INJECTION"
        ),
    )
    await _held_turn(engine, case, 8, "still checking")
    notices = case.turn_history[-1].system_notices
    held = next(i for i, n in enumerate(notices) if n.startswith("RESOLUTION HELD"))
    assert notices.index("MONITOR INJECTION") < held


async def test_momentum_reads_this_turns_stall_count():
    """An expected difference: metrics run after the stall counter, so a turn
    that makes the fifth stall reads BLOCKED on its own record."""
    case, _shown = _pending()
    case.turns_without_progress = 4
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates={}
        )
    )
    await engine.process_turn(case=case, user_message="ok", redaction_ctx=None)
    assert case.turns_without_progress == 5
    assert case.turn_history[-1].momentum == "blocked"


def _record_with_a_token():
    case, _shown = _pending()
    metadata = {"system_notices": ["KEPT"]}
    record_decision(metadata, NoticeCode.FIX_UNTARGETED, action_id="act_1")
    inputs = turn_application.TurnRecordInputs(
        progress_metrics=SimpleNamespace(
            investigation_momentum="moderate", blocked_reasons=[], next_steps=[]
        ),
        stagnation_str=None,
        validation_repairs=[],
        cause_at_start=None,
    )
    turn_application.record_turn(
        case,
        metadata,
        inputs,
        user_message="m",
        response_obj=SimpleNamespace(agent_response="Noted."),
    )
    return case


def test_a_token_left_unrendered_fails_the_test_suite():
    """Review item 11 (second delta): the leak guard fails a test."""
    with pytest.raises(AssertionError, match="left unrendered"):
        _record_with_a_token()


def test_a_token_left_unrendered_is_stripped_in_production(caplog, monkeypatch):
    """Review item 14: in production the token is logged and stripped."""
    from faultmaven.core.investigation.narration import codes

    monkeypatch.setattr(codes, "TOKENS_FAIL", False)
    case = _record_with_a_token()
    assert case.turn_history[-1].system_notices == ["KEPT"]
    assert "left unrendered" in caplog.text


# ---------------------------------------------------------------------------
# The second fix delta (review probes A, B, D, E, F, G, H)
# ---------------------------------------------------------------------------


def _model_proposes(case, metadata, to_state):
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    response = MagicMock()
    response.state_updates.proposed_transition = MagicMock(
        to_state=to_state, evidence_ids=[]
    )
    metadata["response_obj"] = response
    asyncio.run(
        engine.transitions.check_automatic_transitions(case, metadata, to_state)
    )
    metadata.pop("response_obj")


def test_a_lapsed_engine_offer_supersedes_nothing():
    """Probe A: step 2 dropped the model's ``closed`` for the engine's deferred
    offer, and the final recompute lapsed that offer. Nothing is said to stand,
    and the turn does not report the model's proposal superseded."""
    case = _deferred_case()
    metadata = _cycle_metadata()
    _maybe_propose_deferred_close(case, metadata)
    _model_proposes(case, metadata, "closed")
    assert metadata["transition_superseded_by_engine"] is True
    for hypothesis in case.hypotheses.values():
        hypothesis.likelihood = 0.2
    asyncio.run(
        turn_application._final_recompute(
            _responses(), case, metadata, TurnOutcome.CONVERSATION
        )
    )
    notices = narrated(case, metadata)
    assert case.offer_record.open is None
    assert not any("the engine already offered" in n for n in notices)
    assert metadata["transition_superseded_by_engine"] is False
    # Review item 4 (third delta): the model is told, and the turn says why.
    (told,) = [n for n in notices if n.startswith("TRANSITION NOT PROPOSED")]
    assert "your proposal was not opened, and that offer then lapsed" in told
    assert metadata["transition_dropped_for_lapsed_offer"] is True


def test_a_deferred_offer_lapses_on_a_later_turn_whose_licence_fell():
    """Review item 10: one lapse rule, every turn (``offers.lapse_offers``)."""
    from faultmaven.core.investigation.offers import lapse_offers

    case = _deferred_case()
    _maybe_propose_deferred_close(case, _cycle_metadata())
    case.current_turn += 1
    for hypothesis in case.hypotheses.values():
        hypothesis.likelihood = 0.2
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _refresh_working_conclusion,
    )

    _refresh_working_conclusion(case)
    case.offer_record = lapse_offers(case)
    assert case.offer_record.open is None


def test_a_pivot_with_no_record_renders_no_empty_record_block():
    """Probe B: the closure pivot's summary with no outcome."""
    from faultmaven.core.investigation.narration.offers import render_offer

    params = {
        "composer": "closure",
        "verdict": "suggest_resolve",
        "root_cause": "max_connections is 20",
        "root_cause_name": "max_connections is 20",
        "solution_titles": ["raise it"],
        "proposal_id": "p1",
    }
    text = render_offer(OfferCode.MODEL_PROPOSAL, params, None)
    assert "The record shows" not in text
    assert "- **Root cause**: max_connections is 20" in text


def test_two_unbound_entries_render_the_rows_once():
    """Probe D: a replay bundle's code and the live recompute's."""
    _eng, case = _held_recheck()
    decline_revision(case, via=AnswerVia.CLICK)
    metadata = {"milestones_completed": []}
    record_decision(metadata, NoticeCode.UNBOUND_RECHECKS, fix_dated=False)
    record_decision(metadata, NoticeCode.UNBOUND_RECHECKS, fix_dated=True)
    notices = narrated(case, metadata)
    assert len([n for n in notices if _eid("held") in n]) == 1


def test_an_in_memory_pass_counts_nothing_and_narrates_nothing(monkeypatch):
    """Probes E and r4: the cycle's write failed after its fold read a
    disagreement. Neither the failed write nor the narration's in-memory pass
    counts it, and the in-memory pass narrates no row; the next stored write
    counts it once, and the next narration names the row once."""
    from faultmaven.core.investigation import outcome as outcome_module
    from faultmaven.core.investigation.milestone_engine import turn_narration
    from faultmaven.core.investigation.outcome import record_case_outcome
    from tests.unit.core.investigation import golden_cases_2636 as golden

    case, _metadata = golden.an_unnamed_recheck_after_the_fix()
    assert record_case_outcome(case, at="cycle")
    case.progress.outcome.narration_since = 1  # narrated since, no watermark
    action = next(a for a in case.proposed_actions if a.state == "accepted")
    action.accepted_in_turn = 5  # the carried verdict disagrees from here on
    counted = []
    monkeypatch.setattr(
        outcome_module,
        "case_outcome_conflicts_total",
        SimpleNamespace(
            labels=lambda field: SimpleNamespace(inc=lambda: counted.append(field))
        ),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("the write fails after the attempts fold")

    monkeypatch.setattr(outcome_module, "_mitigation", _boom)
    written = record_case_outcome(case, at="cycle")
    assert written is False and counted == []
    monkeypatch.setattr(outcome_module, "_mitigation", lambda *a, **k: None)
    metadata = {"milestones_completed": [], turn_narration.OUTCOME_WRITTEN_KEY: False}
    record_decision(metadata, NoticeCode.UNBOUND_RECHECKS, fix_dated=True)
    narrate_turn(case, metadata)
    assert counted == []
    assert not [n for n in metadata["system_notices"] if "Post-fix" in n]

    case.current_turn += 1
    later = {"milestones_completed": []}
    record_decision(later, NoticeCode.UNBOUND_RECHECKS, fix_dated=True)
    lines = [n for n in narrated(case, later) if "Post-fix" in n]
    assert counted == ["attempts.accepted_in_turn"], "counted once, when stored"
    assert len(lines) == 1, "the row the failed turn left is named on this one"
    assert {u.narrated_turn for u in case.progress.outcome.unbound_rechecks} == {
        case.current_turn
    }


async def test_with_no_stored_record_old_rows_count_as_narrated():
    """Probe F: a case the backfill skipped narrates none of the rows observed
    before its first narrated turn."""
    eng, case = _held_recheck()
    decline_revision(case, via=AnswerVia.CLICK)
    assert case.progress.outcome is None
    case.current_turn = 30
    meta = await _apply(eng, case, _DSU())
    assert not _unbound_line(meta)
    (row,) = case.progress.outcome.unbound_rechecks
    assert row.narrated_turn == 30


def test_a_model_close_names_no_cause_the_record_no_longer_identifies(monkeypatch):
    """Probe G (INV-32): the model's ``closed`` decided on a summary naming the
    identified cause; housekeeping retired its hypothesis and the final
    recompute withdrew the cause. The summary names no cause, and the
    disagreement is counted."""
    from faultmaven.core.investigation.milestone_engine import turn_narration
    from tests.unit.core.investigation import golden_cases_2636 as golden

    counted = []
    monkeypatch.setattr(
        turn_narration,
        "case_outcome_conflicts_total",
        SimpleNamespace(
            labels=lambda field: SimpleNamespace(inc=lambda: counted.append(field))
        ),
    )
    case, _root, hyp = _identified_case()
    golden._pending_fix(case)
    metadata = _cycle_metadata()
    _model_proposes(case, metadata, "closed")
    decided = next(
        e["params"]
        for e in metadata["outcome_decisions"]
        if e["code"] == OfferCode.MODEL_PROPOSAL
    )
    assert decided["root_cause"] is not None, "premise: decided on a cause"
    hyp.state = HypothesisState.RETIRED
    asyncio.run(
        turn_application._final_recompute(
            _responses(), case, metadata, TurnOutcome.CONVERSATION
        )
    )
    narrated(case, metadata)
    assert "Root cause identified" not in case.offer_record.open.summary
    assert "narration.model_proposal" in counted


def test_the_withdrawal_variant_is_read_at_the_record():
    """Probe H, an expected difference: housekeeping retires the hypothesis
    whose root held identification open, so the record, written after it,
    reads the cause fallen."""
    from tests.unit.core.investigation import golden_cases_2636 as golden

    case, metadata = golden.withdrawn_held_open_beside_a_retired_validated_root()
    assert case.progress.cause_identification_held_open, "held open at decision"
    case.hypotheses["hyp_00000000b0b0"].state = HypothesisState.RETIRED
    notices = narrated(case, metadata)
    assert not case.progress.outcome.cause.identification_held_open
    assert notices[0].startswith(
        "SYSTEM: Your pending SOLUTION proposal was withdrawn because"
    )


# ---------------------------------------------------------------------------
# The third fix delta (review probes r1-r4, items 1-10)
# ---------------------------------------------------------------------------


def _counted(monkeypatch) -> list:
    from faultmaven.core.investigation.milestone_engine import turn_narration

    counted: list = []
    monkeypatch.setattr(
        turn_narration,
        "case_outcome_conflicts_total",
        SimpleNamespace(
            labels=lambda field: SimpleNamespace(inc=lambda: counted.append(field))
        ),
    )
    return counted


async def test_two_refusals_in_one_cycle_render_one_line():
    """Probe r3: two applications in one cycle (a replayed bundle's, then the
    live response's) refuse the verify; the first records declared_stable.
    One refusal, one line."""
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _apply_stage_gate_signals,
    )

    _eng, case, _shown = await _accepted()
    case.current_turn = 7
    metadata: dict = {"milestones_completed": []}
    for disposition in ("declared_stable", None):
        signals = MilestoneUpdates(
            mitigation_verified=True, mitigation_disposition=disposition
        )
        _apply_stage_gate_signals(case, signals, "", metadata)
    assert case.progress.mitigation.disposition == "declared_stable"
    refused = [n for n in narrated(case, metadata) if "was not recorded" in n]
    assert len(refused) == 1


async def test_with_no_record_the_refusal_reads_the_disposition_stored_before(
    monkeypatch,
):
    """Item 1: with no outcome at all, a refusal's line reads the disposition
    stored before the cycle, which the decision no longer carries."""
    from faultmaven.core.investigation.milestone_engine import turn_narration

    def _boom(*_a, **_k):
        raise RuntimeError("no outcome")

    _eng, case, _shown = await _accepted()
    case.progress.mitigation.disposition = "proceed_unmitigated"
    monkeypatch.setattr(turn_narration, "compute_case_outcome", _boom)
    metadata = {"milestones_completed": [], turn_narration.OUTCOME_WRITTEN_KEY: False}
    record_decision(
        metadata,
        NoticeCode.MITIGATION_VERIFY_REFUSED,
        reason="no_bound_relief",
        problem_row_ids=[],
    )
    narrate_turn(case, metadata)
    assert metadata["system_notices"] == [_PROCEEDED]


def test_a_cause_identified_on_the_chain_is_no_conflict(monkeypatch):
    """Probe r1: the pivot decided on a cause the chain identifies, with no
    root-cause conclusion to name it; the record identifies it too."""
    from faultmaven.core.investigation.milestone_engine import turn_narration
    from faultmaven.core.investigation.terminal_transitions import (
        assess_closure_readiness,
    )

    counted = _counted(monkeypatch)
    case = _confirmed_case()
    case.root_cause_conclusion = None
    case.progress.cause_state = CauseState.IDENTIFIED
    params = assess_closure_readiness(case).offer_params()
    record = compute_case_outcome(None, case)
    assert (params["root_cause"], record.cause.identification_leg) == (None, "chain")
    turn_narration._conflict(OfferCode.MODEL_PROPOSAL, params, record)
    assert counted == []


async def test_with_no_stored_record_a_row_written_this_turn_is_narrated():
    """Probe r2: the row is written on the first narrated turn, from data
    uploaded on an earlier post-fix turn; it is narrated, not watermarked."""
    eng, case = _held_recheck()
    decline_revision(case, via=AnswerVia.CLICK)
    late = _row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=8,
        evidence_id=_eid("late"),
        summary="checkout answers 200 again",
    )
    late.collected_at_turn = 30
    case.current_turn = 30
    assert case.progress.outcome is None
    meta = await _apply(eng, case, _DSU())
    assert [n for n in meta["system_notices"] if _eid("late") in n]
    assert not _unbound_line(meta), "the row written before stays watermarked"


def test_the_engine_offer_is_read_at_the_end_of_the_cycle():
    """Item 5: an engine offer gone by the end of the cycle without an
    ``OFFER_LAPSED`` code (step 3b's lapse) stands for nothing."""
    from faultmaven.core.investigation.offers import lapse_offers

    case = _deferred_case()
    metadata = _cycle_metadata()
    _maybe_propose_deferred_close(case, metadata)
    _model_proposes(case, metadata, "closed")
    for hypothesis in case.hypotheses.values():
        hypothesis.likelihood = 0.2
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _refresh_working_conclusion,
    )

    _refresh_working_conclusion(case)
    case.offer_record = lapse_offers(case)
    assert case.offer_record.open is None
    notices = narrated(case, metadata)
    assert not any("the engine already offered" in n for n in notices)
    assert metadata["transition_dropped_for_lapsed_offer"] is True


def test_an_in_memory_pass_counts_the_decisions_conflict(monkeypatch):
    """Item 6: with the cycle's write failed, the decision and the outcome
    computed in memory still disagree, and that is counted, once."""
    from faultmaven.core.investigation.milestone_engine import turn_narration

    counted = _counted(monkeypatch)
    case = _confirmed_case()
    metadata = {"milestones_completed": [], turn_narration.OUTCOME_WRITTEN_KEY: False}
    record_decision(
        metadata,
        OfferCode.MODEL_PROPOSAL,
        composer="closure",
        verdict="has_substance",
        evidence_count=2,
        hypothesis_count=0,
        milestones_completed=0,
        root_cause="the identified root cause",
        root_cause_name="the identified root cause",
        identification_leg="rcc",
        solution_titles=[],
    )
    narrate_turn(case, metadata)
    assert counted == ["narration.model_proposal"]


def test_the_lapse_rule_reads_the_predicate_the_deferred_opener_reads(monkeypatch):
    """Item 8: one predicate opens a deferred offer and lapses it."""
    from faultmaven.core.investigation.milestone_engine import stage_gates
    from faultmaven.core.investigation.offers import lapse_offers

    case = _deferred_case()
    _maybe_propose_deferred_close(case, _cycle_metadata())
    assert case.offer_record.open is not None
    monkeypatch.setattr(stage_gates, "_solution_cause_validated", lambda _c: False)
    assert lapse_offers(case).open is None


def test_a_row_whose_line_the_notice_cap_cut_stays_unnarrated():
    """Item 10: the turn record keeps the first notices; a row named only on
    a line it cut is narrated on a later turn."""
    from faultmaven.modules.case.contracts import MAX_SYSTEM_NOTICES

    _eng, case = _held_recheck()
    decline_revision(case, via=AnswerVia.CLICK)
    metadata = {
        "milestones_completed": [],
        "system_notices": [f"NOTICE {n}" for n in range(MAX_SYSTEM_NOTICES)],
    }
    record_decision(metadata, NoticeCode.UNBOUND_RECHECKS, fix_dated=True)
    assert [n for n in narrated(case, metadata) if _eid("held") in n]
    (row,) = case.progress.outcome.unbound_rechecks
    assert row.narrated_turn is None
