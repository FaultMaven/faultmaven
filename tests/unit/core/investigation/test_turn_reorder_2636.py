"""Outcome notices after the record (#2636 B1b, #2615 stage R).

Within a decision cycle the withdrawal, the mitigation verify and disposition
refusals and the refutation refusal are composed only by the narration step,
from the cycle's decision log and the record ``record_case_outcome`` wrote.
Pin 4 drives a turn whose step 1 refuses and whose step 4 retires through
``process_turn``. Every text is ``main``'s, captured by running ``main``
(``golden_cases_2636.CYCLE_SCENARIOS``, ``golden_main_2636.json``), except the
four ruled differences, each asserted against a captured oracle: #2603, A19,
the withdrawal variant read at the record, and one line for two equal
decisions (A9). No live LLM: the generator is stubbed.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.milestone_engine import turn_application
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    merge_decisions,
    merge_system_notices,
    record_decision,
)
from faultmaven.core.investigation.narration.codes import DECISIONS_KEY, NoticeCode
from faultmaven.core.investigation.narration.notices import render_notice
from faultmaven.core.investigation.outcome import compute_case_outcome
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
    EvidenceCategory,
    HypothesisState,
    PoolEmptyReason,
    ProblemStatus,
    TurnOutcome,
)
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation.bound_rows import bound_row
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_mitigation_record_2100 import (
    _accepted,
    _feedback,
    _pending,
    _turn,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _DSU,
    _standing,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import _make_repo

pytestmark = pytest.mark.unit

#: ``main``'s texts, captured by running ``main`` (``golden_cases_2636``).
ORACLE = json.loads(golden.GOLDEN.read_text())


def oracle(name: str) -> list[str]:
    return ORACLE[f"cycle:{name}"]


def rendered(name: str) -> list[str]:
    """What this tree tells the model for the scenario: the record, then the
    narration step."""
    case, metadata = golden.CYCLE_SCENARIOS[name]()
    return golden.cycle_text(narrated(case, metadata))


#: The ruled text differences (#2636, 2026-11-11), each asserted below.
DECLARED = {
    "mitigation_no_bound_relief_beside_a_proceed",  # #2603
    "mitigation_refused_twice_in_one_cycle",  # A9
}


@pytest.mark.parametrize("name", sorted(set(golden.CYCLE_SCENARIOS) - DECLARED))
def test_each_notice_is_the_text_main_composed(name):
    assert rendered(name) == oracle(name)


def test_every_cycle_scenario_was_captured_on_main():
    assert {f"cycle:{n}" for n in golden.CYCLE_SCENARIOS} <= set(ORACLE)


# ---------------------------------------------------------------------------
# Pin 4: the turn order, through process_turn
# ---------------------------------------------------------------------------


def _deadlock(case):
    """The progress monitor's deadlock repair, as step 4 runs it: it retires
    every INCONCLUSIVE hypothesis."""
    repair = ProgressMonitor()._repair_deadlock(case)
    return ProgressCheckResult(
        transparent_mode=True,
        repair_type=repair.repair_type,
        repair_action=repair.action,
        repair_message=repair.message,
        prompt_injection=repair.prompt_injection,
    )


async def test_pin_4_a_turn_that_refuses_then_retires_renders_one_text(monkeypatch):
    """Step 1a refuses the mitigation's verify (no bound relief), registers the
    same response's ``proceed_unmitigated`` and refuses an unfounded
    refutation; step 4's deadlock repair retires the hypothesis that
    refutation named. Each line states where the cycle ended: no re-check is
    asked of a proceeded mitigation (#2603), and a retired hypothesis is not
    said to stay open (A19)."""
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

    assert case.hypotheses[h_id].state == HypothesisState.RETIRED
    assert case.progress.outcome.mitigation.disposition == "proceed_unmitigated"
    notices = case.turn_history[-1].system_notices
    mitigation = [n for n in notices if "mitigation_verified was not" in n]
    assert mitigation == oracle("mitigation_proceeded")
    refused = [golden.cycle_text([n])[0] for n in notices if " was NOT refuted" in n]
    (open_line,) = oracle("refutation_refused_open")
    assert refused == [open_line.replace(" It stays open.", "")]
    assert not any("\u2063" in n for n in notices), "every token was rendered"


async def test_the_turn_record_reads_what_main_read_at_step_6(monkeypatch):
    """Review item 7: the record is created after the narration, but every
    field but its notices is what step 6 read. A step after it changing the
    metadata changes no field of the record."""
    case, _shown = _pending()
    case.current_turn = 6
    seen: dict = {}
    real = turn_application._record_hypotheses_closed_later

    def _then_change(case, metadata):
        real(case, metadata)
        metadata.setdefault("milestones_completed", []).append("after_step_6")
        metadata.setdefault("validation_repairs", []).append("after_step_6")
        metadata["progress_made"] = not metadata.get("progress_made", False)
        seen.update(metadata)

    monkeypatch.setattr(
        turn_application, "_record_hypotheses_closed_later", _then_change
    )
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates={}
        )
    )
    await engine.process_turn(case=case, user_message="ok", redaction_ctx=None)
    record = case.turn_history[-1]
    assert "after_step_6" in seen["milestones_completed"], "premise: it ran"
    assert "after_step_6" not in record.milestones_completed
    assert "after_step_6" not in record.validation_repairs
    assert record.progress_made != seen["progress_made"]


# ---------------------------------------------------------------------------
# The declared differences
# ---------------------------------------------------------------------------


def test_2603_a_refused_verify_beside_a_proceed_asks_for_no_recheck():
    """``main`` asked for a re-check the proceed made impossible."""
    assert (
        "record the post-mitigation"
        in oracle("mitigation_no_bound_relief_beside_a_proceed")[0]
    )
    assert rendered("mitigation_no_bound_relief_beside_a_proceed") == oracle(
        "mitigation_proceeded"
    )


async def test_2603_through_the_apply_step():
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
    assert golden.cycle_text(meta["system_notices"]) == oracle("mitigation_proceeded")
    assert "record the post-mitigation" not in _feedback(meta)


def test_a9_two_equal_refusals_in_one_cycle_render_one_line():
    first, second = oracle("mitigation_refused_twice_in_one_cycle")
    assert first == second, "premise: main wrote the same line twice"
    assert rendered("mitigation_refused_twice_in_one_cycle") == [first]


def test_a_decision_recorded_twice_holds_one_place():
    metadata: dict = {}
    for _ in range(2):
        record_decision(
            metadata,
            NoticeCode.REFUTATION_REFUSED,
            hypothesis_id="hyp_1",
            hedged=False,
            retired=False,
        )
        record_decision(metadata, NoticeCode.SOLUTION_OFFER_WITHDRAWN, held_open=False)
    assert len(metadata["system_notices"]) == 2


def test_a_refusal_and_a_proceed_rendering_one_text_render_it_once():
    """Critique 3: a replayed bundle's refused verify beside its proceed, then
    the live response's verify. Two codes render one text; the turn repeats
    no notice, as ``main``'s merge keeps it."""
    case, metadata = golden.mitigation_no_bound_relief_beside_a_proceed()
    golden._signals(case, 7, metadata, mitigation_verified=True)
    assert golden.cycle_text(narrated(case, metadata)) == oracle("mitigation_proceeded")


def test_the_withdrawal_variant_is_read_at_the_record():
    """A later recompute of the cycle (a later replayed bundle's, or the live
    response's) finds the holding hypothesis retired, so the record, written
    after it, reads the cause fallen."""
    case, metadata = golden.withdrawn_held_open_beside_a_retired_validated_root()
    assert case.progress.cause_identification_held_open, "held open at decision"
    case.hypotheses["hyp_00000000b0b0"].state = HypothesisState.RETIRED
    _recompute_assessment_state(case, metadata={"milestones_completed": []})
    notices = narrated(case, metadata)
    assert not case.progress.outcome.cause.identification_held_open
    assert notices == oracle("withdrawn_once_the_cause_fell")


def test_the_record_agrees_with_the_flag_its_readers_read():
    """Review item 1: a held-open cause whose hypothesis a later step of the
    cycle retires. No recompute has read the retirement, so the record's flag,
    the pool's empty reason and the progress flag the next prompt reads agree,
    and the withdrawal is the held-open variant."""
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _withdraw_unlicensed_solution_offers,
    )
    from faultmaven.modules.case.contracts import (
        InvestigationActionType,
        ProposedAction,
        WorkingConclusion,
    )
    from tests.unit.core.investigation.test_terminal_root_identification_2036 import (
        _open_chain_case,
        _recompute_cause_state_from_chain,
    )

    case, _root, hyp = _open_chain_case()
    _recompute_cause_state_from_chain(case)
    case.working_conclusion = WorkingConclusion(
        statement=hyp.statement, reasoning="r", likelihood=0.9
    )
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="generate a fresh secret_id",
            proposed_in_turn=case.current_turn,
        )
    )
    metadata: dict = {}
    assert _withdraw_unlicensed_solution_offers(case, metadata=metadata) == 1
    hyp.state = HypothesisState.RETIRED
    notices = narrated(case, metadata)
    cause = case.progress.outcome.cause
    assert case.progress.cause_identification_held_open is True
    assert cause.pool_empty_reason == PoolEmptyReason.HELD_OPEN
    assert cause.identification_held_open is True
    assert notices == oracle("withdrawn_held_open_beside_a_retired_validated_root")


def test_a19_a_refusal_whose_hypothesis_a_later_step_retired():
    """Critique 5: any refusal in the cycle's log (a replayed bundle's
    included) whose hypothesis is retired at the end of the cycle."""
    case, metadata = golden.CYCLE_SCENARIOS["refutation_refused_open"]()
    (h_id,) = case.hypotheses
    case.hypotheses[h_id].state = HypothesisState.RETIRED
    turn_application._record_hypotheses_closed_later(case, metadata)
    (open_line,) = oracle("refutation_refused_open")
    assert golden.cycle_text(narrated(case, metadata)) == [
        open_line.replace(" It stays open.", "")
    ]


@pytest.mark.parametrize(
    ("state", "stays_open"),
    [
        (HypothesisState.REFUTED, False),
        (HypothesisState.VALIDATED, False),
        (HypothesisState.INCONCLUSIVE, True),
    ],
)
def test_a19_a_refusal_whose_hypothesis_the_cycle_ends_not_open(state, stays_open):
    """Review item 6: refuted or validated later in the cycle (the M6
    demotion, a later bundle, the live response) is not open either;
    INCONCLUSIVE stays open, as on ``main``."""
    case, metadata = golden.CYCLE_SCENARIOS["refutation_refused_open"]()
    (h_id,) = case.hypotheses
    case.hypotheses[h_id].state = state
    turn_application._record_hypotheses_closed_later(case, metadata)
    (open_line,) = oracle("refutation_refused_open")
    expected = open_line if stays_open else open_line.replace(" It stays open.", "")
    assert golden.cycle_text(narrated(case, metadata)) == [expected]


def test_the_refusal_renders_the_branch_decided():
    """Critique 2 (A4): a refusal decided on an unverified problem that the
    cycle then verifies still says why it was refused."""
    case, metadata = golden.mitigation_problem_unverified()
    case.progress.problem_status = ProblemStatus.VERIFIED
    assert golden.cycle_text(narrated(case, metadata)) == oracle(
        "mitigation_problem_unverified"
    )


def test_the_verify_refusal_reads_its_turns_from_the_record():
    """Review item 2: the problem is seen present after the refusal was
    decided, later in the cycle. The record holds that turn, the decider's
    params do not, and the line follows the record."""
    case, metadata = golden.mitigation_no_bound_relief()
    (entry,) = metadata[DECISIONS_KEY]
    assert entry["params"]["present_turn"] is None, "premise: decided before"
    bound_row(case, EvidenceCategory.SYMPTOM_EVIDENCE, turn=8)
    (line,) = narrated(case, metadata)
    assert case.progress.outcome.mitigation.present_turn == 8
    assert "(turn 6) (it was seen present at turn 8): record" in line


def test_the_disposition_refusal_reads_the_disposition_from_the_record():
    """Review item 2: the record's disposition, not the one decided."""
    case, metadata = golden.disposition_carries()
    (entry,) = [e for e in metadata[DECISIONS_KEY] if e["params"].get("why")]
    assert entry["params"]["carried"] == "declared_stable"
    case.progress.mitigation.disposition = "proceed_unmitigated"
    (line,) = narrated(case, metadata)
    assert "already carries proceed_unmitigated, written once" in line


def test_an_aggregated_decision_takes_the_later_params(monkeypatch):
    """Review item 5: a replayed bundle withdrew while identification was held
    open, the live response after the cause fell. With no outcome, the line
    is the later decision's, merged or recorded in one accumulator."""
    case, _metadata = golden.withdrawn_once_the_cause_fell()
    bundle: dict = {}
    record_decision(
        bundle, NoticeCode.SOLUTION_OFFER_WITHDRAWN, prepend=True, held_open=True
    )
    live: dict = {}
    record_decision(
        live, NoticeCode.SOLUTION_OFFER_WITHDRAWN, prepend=True, held_open=False
    )
    turn = {**bundle, DECISIONS_KEY: list(bundle[DECISIONS_KEY])}
    merge_system_notices(turn, live)
    merge_decisions(turn, live)
    _no_outcome(monkeypatch)
    assert narrated(case, turn) == oracle("withdrawn_once_the_cause_fell")
    same: dict = {}
    for held_open in (True, False):
        record_decision(same, NoticeCode.SOLUTION_OFFER_WITHDRAWN, held_open=held_open)
    assert narrated(case, same) == oracle("withdrawn_once_the_cause_fell")


def _no_outcome(monkeypatch) -> None:
    """A derivation the write and the in-memory pass share raises."""

    def _boom(*_a, **_k):
        raise RuntimeError("no outcome")

    monkeypatch.setattr(outcome_module, "shown_milestones", _boom)


# ---------------------------------------------------------------------------
# The failed write (ruled (a))
# ---------------------------------------------------------------------------


def _counted(monkeypatch) -> list:
    counted: list = []
    monkeypatch.setattr(
        outcome_module,
        "case_outcome_conflicts_total",
        SimpleNamespace(
            labels=lambda field: SimpleNamespace(inc=lambda: counted.append(field))
        ),
    )
    return counted


@pytest.mark.parametrize(
    "name",
    [
        "withdrawn_held_open_beside_a_retired_validated_root",
        "mitigation_no_bound_relief_seen_present",
    ],
)
def test_with_no_outcome_the_notices_are_mains(monkeypatch, name):
    """A derivation the write and the in-memory pass share raises, so neither
    yields an outcome: each notice falls back on what its decider recorded
    (the held-open variant, the turn clauses) and is ``main``'s text."""
    case, metadata = golden.CYCLE_SCENARIOS[name]()
    _no_outcome(monkeypatch)
    assert golden.cycle_text(narrated(case, metadata)) == oracle(name)
    assert case.progress.outcome is None


def test_a_failed_write_narrates_from_an_outcome_never_stored(monkeypatch):
    """The write fails once (a transient error); the narration's in-memory
    outcome reads the record's verdicts and is stored nowhere."""
    case, metadata = golden.mitigation_no_bound_relief_seen_present()
    real = outcome_module.shown_milestones
    calls: list = []

    def _once(case):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return real(case)

    monkeypatch.setattr(outcome_module, "shown_milestones", _once)
    assert golden.cycle_text(narrated(case, metadata)) == oracle(
        "mitigation_no_bound_relief_seen_present"
    )
    assert len(calls) == 2, "the write, then the narration's own computation"
    assert case.progress.outcome is None


def test_an_outcome_computed_in_memory_counts_no_disagreement(monkeypatch):
    """With ``unreported`` the fold collects what it first reads and counts
    nothing; without it, it counts as it always has."""
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
        _deepen,
        _fix,
        _identified_case,
        _recompute_at,
        _relapse,
    )
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _eid,
    )

    counted = _counted(monkeypatch)
    case, root, hyp = _identified_case()
    _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    first = compute_case_outcome(None, case)
    _deepen(case, hyp, root)
    found: list = []
    compute_case_outcome(first, case, unreported=found)
    assert counted == [] and [f[0] for f in found] == ["attempts.window"]
    compute_case_outcome(first, case)
    assert counted == ["attempts.window"]


def test_with_no_outcome_a_proceeded_mitigation_asks_for_no_recheck():
    """The disposition comes from the log when no outcome can be read."""
    params = {
        "reason": "no_bound_relief",
        "problem_row_ids": ["ev_p"],
        "attempt_turn": 6,
        "present_turn": None,
    }
    disposed = {
        "code": NoticeCode.MITIGATION_DISPOSED,
        "params": {"value": "proceed_unmitigated"},
    }
    code = NoticeCode.MITIGATION_VERIFY_REFUSED
    assert render_notice(code, params, None, [disposed]) == oracle(
        "mitigation_proceeded"
    )


# ---------------------------------------------------------------------------
# Tokens and codes
# ---------------------------------------------------------------------------


def _record_with_a_token():
    case, _shown = _pending()
    metadata = {"system_notices": ["KEPT"]}
    record_decision(metadata, NoticeCode.MITIGATION_VERIFY_PROCEEDED)
    inputs = turn_application.TurnRecordInputs(
        fields=dict(
            turn_number=case.current_turn,
            milestones_completed=[],
            evidence_added=[],
            hypotheses_generated=[],
            hypotheses_validated=[],
            solutions_proposed=[],
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
            user_message="m",
            agent_response="Noted.",
        ),
        stagnation_str=None,
        validation_repairs=[],
    )
    turn_application.record_turn(case, metadata, inputs)
    return case


def test_a_token_left_unrendered_fails_the_test_suite():
    with pytest.raises(AssertionError, match="left unrendered"):
        _record_with_a_token()


def test_a_token_left_unrendered_is_stripped_in_production(caplog, monkeypatch):
    from faultmaven.core.investigation.narration import codes

    monkeypatch.setattr(codes, "TOKENS_FAIL", False)
    case = _record_with_a_token()
    assert case.turn_history[-1].system_notices == ["KEPT"]
    assert "left unrendered" in caplog.text


def test_the_codes_are_append_only():
    """B1c and B1d append theirs after these."""
    assert [m.value for m in NoticeCode] == [
        "solution_offer_withdrawn",
        "mitigation_verify_proceeded",
        "mitigation_verify_refused",
        "mitigation_disposition_refused",
        "refutation_refused",
        "hypothesis_retired",
        "mitigation_disposed",
    ]
