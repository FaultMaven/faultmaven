"""A failed fix disconfirms a cause only through a pair of that cause's own
rows (#2586; #3046 S3b, stage S3 of #2615).

#2586's sequence: A's kinded fix fails and M6 demotes A (8); B, older than
the fix, is identified; B's kinded fix runs at 10; A's row is re-checked
gone at 11, the problem is present at 12, and B's own row is re-checked gone
at 13. ``main`` refuted B at 13 on the pair (11, 12), read through A's row.
A kinded window's pair now reads only its targets' rows, M6 attributes
through the pair over this cause's own rows, the record holds a FAILED
window's problem rows final at its failure, and a demotion on a failed fix
is recorded with its pair (``CaseOutcome.demotions``). No live LLM.
"""

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    _fix_failure_refusal,
    failed_fix_of,
    record_demotion,
)
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    carried_fix_windows,
    fix_windows,
    latest_decided_fix_window,
    subject_index,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.lifecycle_metrics import _M6_REFUSAL_REASONS
from faultmaven.core.investigation.milestone_engine import stage_gates
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_hypothesis_action_intent,
    _apply_pending_refutations,
    _refutation_grounds,
)
from faultmaven.core.investigation.narration.codes import DECISIONS_KEY
from faultmaven.core.investigation.outcome import record_case_outcome
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
)
from faultmaven.core.investigation.terminal_transitions import (
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    HypothesisState,
    InvestigationActionType,
    RecheckReason,
    WritePoint,
)
from tests.unit.core.investigation import test_cause_leg_cited_basis_2557 as cited
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_case_outcome_fold_2636 import _deepen
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _accept,
    _eid,
    _engine_rows,
    _recompute_at,
)

pytestmark = pytest.mark.unit


def _cycle(case, turn) -> None:
    """A cycle's end: the recompute, then the record (#2636)."""
    _recompute_at(case, turn)
    record_case_outcome(case, at=WritePoint.CYCLE)


def _b_fixed():
    """A demoted at 8, B identified, B's kinded fix at 10."""
    case, b, hyp_b, rows = cited._kinded_a_fell_older_b_stands()
    record_case_outcome(case, at=WritePoint.CYCLE)
    fix = cited._fix(case, 10, "remediation", [b.node_id], cited._FIX)
    return case, b, hyp_b, rows, fix


def _present(case, turn, label) -> None:
    case.evidence.append(cited._row(label, cited._PRESENT, turn, "503s again"))


def _failed_on_a(*, recheck_b=13):
    """#2586's sequence up to B's own re-check (13): A's row re-checked at 11,
    the problem present at 12."""
    case, b, hyp_b, rows, fix = _b_fixed()
    cited._recheck(case, _eid("pool_log"), turn=11, label="rc_a2")
    _present(case, 12, "back2")
    _cycle(case, 12)
    rc_b = cited._recheck(case, rows[0].evidence_id, turn=recheck_b, label="rc_b")
    return case, b, hyp_b, fix, rc_b


def _record_window(case, fix):
    (attempt,) = [
        a for a in case.progress.outcome.attempts if a.action_id == fix.action_id
    ]
    return attempt.window


# ---------------------------------------------------------------------------
# S1, #2586's report: B stands at 13
# ---------------------------------------------------------------------------


def test_2586_a_failure_read_on_another_roots_row_leaves_the_cause_standing():
    case, _b, hyp_b, fix, _rc_b = _failed_on_a()

    _cycle(case, 13)

    window = latest_decided_fix_window(case)
    assert (window.fix_turn, window.state, window.failure_turn) == (10, FIX_FAILED, 12)
    assert window.disconfirmation is None and window.relief_turn is None
    assert {e.evidence_id for e in window.cause_rows} >= {_eid("rc_a2")}, "premise"
    assert hyp_b.state == HypothesisState.VALIDATED
    assert [e.collected_at_turn for e in _engine_rows(case)] == [8]
    assert _record_window(case, fix).disconfirmation is None
    assert case.progress.outcome.demotions == []
    assert _fix_failure_refusal(case, window) == "no_own_cause_removal"
    assert "no_own_cause_removal" in _M6_REFUSAL_REASONS


def test_2586_a_model_refutation_of_b_is_refused_and_told():
    case, _b, hyp_b, _fix_b, _rc_b = _failed_on_a()
    _cycle(case, 13)
    assert _refutation_grounds(case, hyp_b) == ([], None)
    metadata = {"pending_refutations": [(hyp_b.hypothesis_id, "the fix failed")]}

    _apply_pending_refutations(HypothesisManager(), case, metadata, 13)

    assert hyp_b.state != HypothesisState.REFUTED
    (notice,) = narrated(case, metadata)
    assert notice.startswith(
        f"Hypothesis {hyp_b.hypothesis_id} was NOT refuted: nothing on record "
        "refutes it."
    )
    assert "or on a recorded failed fix of it." in notice
    assert case.progress.outcome.demotions == []


def test_2586_the_absence_notice_says_the_fixs_own_cause_was_not_shown_removed():
    """The window FAILED with neither a relief nor a pair: the notice's third
    shape (critique 1), never "seen gone at turn None"."""
    case, _b, _hyp_b, _fix_b, _rc_b = _failed_on_a()
    _recompute_at(case, 13)
    metadata: dict = {}

    assert stage_gates._flag_absence_inside_a_failed_fix_window(case, metadata)
    (notice,) = narrated(case, metadata)

    assert notice.startswith(
        "SYSTEM: The fix executed at turn 10 did not hold: the problem was seen "
        "again at turn 12, before the cause it addressed was shown removed. "
    )
    assert "turn None" not in notice
    assert (
        " — that does not disprove the cause the fix addressed: if that cause "
        "still stands, no new hypothesis is needed; if it no longer stands, "
        "re-assert it with a root_cause_conclusion on a new hypothesis — then "
        "record the cause gone and the problem gone after it." in notice
    )


def test_2586_what_the_record_and_the_prompt_read_at_13():
    """Critique 6's moves, pinned: the re-checks' reasons, the newest failed
    fix the engine knows, readiness and the fix-verification block."""
    case, _b, _hyp_b, _fix_b, rc_b = _failed_on_a()
    _cycle(case, 13)
    cause = case.progress.outcome.cause

    assert cause.recheck == {
        _eid("rc_a"): RecheckReason.PREDATES_FAILED_FIX,
        _eid("rc_a2"): RecheckReason.NO_CONFIRMING_WINDOW,
        rc_b.evidence_id: RecheckReason.NO_CONFIRMING_WINDOW,
    }
    assert cause.latest_disconfirmation_turn == 8
    assert "root cause" not in assess_resolution_readiness(case).missing
    block = build_fix_verification_block(case)
    assert (
        f"  - cause: missing; re-checked, not counted yet: {_eid('rc_a')} (its "
        f"data predates the failed fix seen at turn 8), {_eid('rc_a2')} (the "
        "fix executed at turn 10 was seen failing at turn 12; only a new fix "
        f"can confirm), {rc_b.evidence_id} (the fix executed at turn 10 was "
        "seen failing at turn 12)\n" in block
    )


# ---------------------------------------------------------------------------
# S2: B's own fix fails too, and the demotion is recorded with its pair
# ---------------------------------------------------------------------------


def test_2586_b_falls_to_its_own_pair_and_the_demotion_is_recorded():
    case, b, hyp_b, fix, rc_b = _failed_on_a()
    _cycle(case, 13)
    _present(case, 14, "back3")

    _cycle(case, 14)

    assert hyp_b.state == HypothesisState.REFUTED
    rows = _engine_rows(case)
    assert [e.collected_at_turn for e in rows] == [8, 14]
    assert "removed at turn 13" in rows[-1].summary
    assert "at turn 14" in rows[-1].summary
    window = _record_window(case, fix)
    pair = window.disconfirmation
    assert (pair.cause_turn, pair.problem_turn) == (13, 14)
    assert pair.cause_row_id == rc_b.evidence_id
    assert pair.cause_row_id in window.cause_row_ids
    assert window.failure_turn == 12
    (demotion,) = case.progress.outcome.demotions
    assert (demotion.hypothesis_id, demotion.action_id) == (
        hyp_b.hypothesis_id,
        fix.action_id,
    )
    assert (demotion.root_id, demotion.fix_turn, demotion.turn) == (b.node_id, 10, 14)
    assert demotion.cause_row_id == rc_b.evidence_id
    assert demotion.cause_row_id in window.cause_row_ids
    assert demotion.problem_row_id == _eid("back3")


def test_the_models_own_failed_fix_refutation_records_the_demotion_once():
    """Amendment 5: the model refutes B in the failing turn on the failed fix;
    M6 then reads the evidence kind and records nothing, so the refutation
    records it, once per hypothesis and attempt."""
    case, _b, hyp_b, fix, rc_b = _failed_on_a()
    _cycle(case, 13)
    _present(case, 14, "back3")
    case.current_turn = 14
    window = failed_fix_of(case, hyp_b)
    metadata = {"pending_refutations": [(hyp_b.hypothesis_id, "the fix failed")]}

    _apply_pending_refutations(HypothesisManager(), case, metadata, 14)
    record_demotion(case, hyp_b, window)
    _cycle(case, 14)

    assert hyp_b.state == HypothesisState.REFUTED
    assert [e.collected_at_turn for e in _engine_rows(case)] == [8, 14]
    (demotion,) = case.progress.outcome.demotions
    assert (demotion.action_id, demotion.fix_turn) == (fix.action_id, 10)
    assert (demotion.cause_row_id, demotion.problem_row_id) == (
        rc_b.evidence_id,
        _eid("back3"),
    )


def test_with_no_record_nothing_is_recorded(caplog):
    case, _b, hyp_b, _fix_b, _rc_b = _failed_on_a()
    _cycle(case, 13)
    _present(case, 14, "back3")
    case.progress.outcome = None

    _recompute_at(case, 14)

    assert hyp_b.state == HypothesisState.REFUTED
    assert case.progress.outcome is None
    assert "is not recorded: no outcome record" in caplog.text


def test_a_recorded_demotion_is_final_across_a_re_root():
    case, b, hyp_b, _fix_b, _rc_b = _failed_on_a()
    _cycle(case, 13)
    _present(case, 14, "back3")
    _cycle(case, 14)
    (before,) = case.progress.outcome.demotions

    _deepen(case, hyp_b, b, turn=15)
    _cycle(case, 15)

    assert case.progress.outcome.demotions == [before]


# ---------------------------------------------------------------------------
# S3 and S4: a relapse, and the control
# ---------------------------------------------------------------------------


def test_a_relapse_over_another_roots_recheck_leaves_b_standing():
    """B re-checked at 11, the problem gone at 12, A's row re-checked at 13,
    the problem present at 14: ``main`` refuted B on (13, 14), A's row."""
    case, _b, hyp_b, rows, _fix_b = _b_fixed()
    cited._recheck(case, rows[0].evidence_id, turn=11, label="rc_b")
    case.evidence.append(cited._row("gone12", cited._GONE, 12, "200s now"))
    _cycle(case, 12)
    cited._recheck(case, _eid("pool_log"), turn=13, label="rc_a2")
    _present(case, 14, "back4")

    _cycle(case, 14)

    window = latest_decided_fix_window(case)
    assert (window.relief_turn, window.failure_turn, window.disconfirmation) == (
        12,
        14,
        None,
    )
    assert _fix_failure_refusal(case, window) == "relapse"
    assert hyp_b.state == HypothesisState.VALIDATED
    assert case.progress.outcome.demotions == []


def test_the_control_b_falls_on_its_own_recheck():
    case, _b, hyp_b, rows, fix = _b_fixed()
    cited._recheck(case, rows[0].evidence_id, turn=11, label="rc_b")
    _present(case, 12, "back5")

    _cycle(case, 12)

    assert hyp_b.state == HypothesisState.REFUTED
    assert [e.collected_at_turn for e in _engine_rows(case)] == [8, 12]
    (demotion,) = case.progress.outcome.demotions
    assert (demotion.action_id, demotion.fix_turn, demotion.turn) == (
        fix.action_id,
        10,
        12,
    )


# ---------------------------------------------------------------------------
# Q3: a kinded window's own pair
# ---------------------------------------------------------------------------


def test_a_kinded_windows_pair_ignores_an_unrelated_fallen_roots_row():
    case, _b, _hyp_b, _fix_b, _rc_b = _failed_on_a()
    window = fix_windows(case)[-1]
    held = {e.evidence_id for e in window.cause_rows}
    assert _eid("rc_a2") in held, "premise: the row fails the window"
    assert window.disconfirmation is None
    assert window.pair_through(
        subject_index(case).observed(e) for e in window.cause_rows
    ) == (11, 12), "premise: the row would pair"


def test_a_recheck_of_a_row_the_target_cites_forms_its_pair():
    """Amendment 4: the targets' lineage reaches the pair through
    ``cited_by(own)``: B's verdict cites a row nothing links, re-checked gone
    at 11 with the problem present at 12."""
    why = cited._row("why_b", cited._CAUSAL, 9, "the worker pool was never recycled")
    case, b, hyp_b, _rows = cited._kinded_a_fell_older_b_stands(b_cites=[why])
    case.evidence.append(why)
    linked = {
        link.evidence_id
        for n in case.causal_nodes.values()
        for link in n.evidence_links
    }
    assert why.evidence_id not in linked, "premise: cited, not linked"
    record_case_outcome(case, at=WritePoint.CYCLE)
    cited._fix(case, 10, "remediation", [b.node_id], cited._FIX)
    cited._recheck(case, why.evidence_id, turn=11, label="rc_why")
    _present(case, 12, "back6")

    _cycle(case, 12)

    assert fix_windows(case)[-1].disconfirmation == (11, 12)
    assert hyp_b.state == HypothesisState.REFUTED
    (demotion,) = case.progress.outcome.demotions
    assert demotion.cause_row_id == _eid("rc_why")


def test_the_report_dating_guard_reads_a_kinded_windows_own_pair():
    """B's row re-checked and the problem present at 14, a fix registered at
    15 and reported at 14: dated to 14, B's row would leave the window and its
    pair (14, 14) with it, so the declaration is ignored. ``main`` read the
    pair (11, 12) on A's row both ways and dated it."""
    case, _b, _hyp_b, rows, _fix_b = _b_fixed()
    cited._recheck(case, _eid("pool_log"), turn=11, label="rc_a2")
    _present(case, 12, "back2")
    _cycle(case, 12)
    cited._recheck(case, rows[0].evidence_id, turn=14, label="rc_b")
    _present(case, 14, "back7")
    _cycle(case, 14)
    case.current_turn = 15
    _accept(case, InvestigationActionType.SOLUTION, 15)
    target = case.proposed_actions[-1]
    assert fix_windows(case)[-2].disconfirmation == (14, 14), "premise"
    metadata: dict = {}

    stage_gates._date_to_the_report(case, target, metadata)

    assert target.accepted_in_turn == 15
    (entry,) = metadata[DECISIONS_KEY]
    assert entry["params"]["reason"] == "decides_disconfirmation"


# ---------------------------------------------------------------------------
# Q1: the carried FAILED window's problem rows are its record's
# ---------------------------------------------------------------------------


def test_a_carried_failed_windows_problem_rows_stay_its_records_after_a_relief():
    """An unkinded fix relapses (relief at 7, failure at 9); after a re-root,
    the problem is gone again at 12. The fresh read holds that row; the
    carried window holds the rows the failure was read on."""
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
        _fix,
        _relapse,
    )
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _identified_case,
    )

    case, root, hyp = _identified_case()
    fix = _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _cycle(case, 9)
    recorded = _record_window(case, fix).problem_row_ids
    assert recorded, "premise: the relief the failure read"
    _deepen(case, hyp, root)
    case.evidence.append(cited._row("relief2", cited._GONE, 12, "checkout answers 200"))
    case.current_turn = 12
    fresh = {e.evidence_id for e in fix_windows(case)[-1].problem_rows}
    assert _eid("relief2") in fresh, "premise: the fresh read holds the later relief"

    window = carried_fix_windows(case)[-1]

    assert window.state == FIX_FAILED
    assert [e.evidence_id for e in window.problem_rows] == recorded


# ---------------------------------------------------------------------------
# The per-root pair is the window's pair over all its rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sequence", ["control", "s2", "relapse"])
def test_pair_through_every_cause_row_is_the_windows_pair(sequence):
    if sequence == "control":
        case, _b, _hyp, rows, _fix_b = _b_fixed()
        cited._recheck(case, rows[0].evidence_id, turn=11, label="rc_b")
        _present(case, 12, "back5")
    elif sequence == "s2":
        case, _b, _hyp, _fix_b, _rc_b = _failed_on_a()
        _present(case, 14, "back3")
    else:
        from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
            _fix,
            _relapse,
        )
        from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
            _identified_case,
        )

        case, _root, _hyp = _identified_case()
        _fix(case)
        _relapse(case, rechecks=_eid("pool_log"))
    subjects = subject_index(case)
    for window in fix_windows(case, subjects):
        if window.kind is None:
            turns = (subjects.observed(e) for e in window.cause_rows)
            assert window.pair_through(turns) == window.disconfirmation


def test_failed_fix_of_names_the_attributed_roots_own_row():
    case, _b, hyp_b, _fix_b, rc_b = _failed_on_a()
    _cycle(case, 13)
    _present(case, 14, "back3")
    case.current_turn = 14

    window = failed_fix_of(case, hyp_b)

    assert (window.m6_pair, window.pair_cause_row_id) == ((13, 14), rc_b.evidence_id)


# ---------------------------------------------------------------------------
# The review of 061ead669
# ---------------------------------------------------------------------------


def test_the_records_pair_names_the_row_it_was_read_through():
    """Item 1: A's row and B's row re-checked at 13, the problem present at
    14. The pair (13, 14) is read through B's row; the record names it, not
    A's row of the same turn."""
    case, _b, hyp_b, rows, fix = _b_fixed()
    cited._recheck(case, _eid("pool_log"), turn=13, label="rc_a3")
    rc_b = cited._recheck(case, rows[0].evidence_id, turn=13, label="rc_b")
    _present(case, 14, "back3")

    _cycle(case, 14)

    assert hyp_b.state == HypothesisState.REFUTED
    window = _record_window(case, fix)
    assert window.disconfirmation.cause_row_id == rc_b.evidence_id
    (demotion,) = case.progress.outcome.demotions
    assert demotion.cause_row_id == rc_b.evidence_id


def test_a_failed_windows_minted_problem_rows_stop_at_its_failure():
    """Item 3: an unkinded relapse (relief 8, failure 9) and a relief row at
    10, before the first record: the record holds only the turn-8 relief."""
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
        _fix,
        _relapse,
    )
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _identified_case,
    )

    case, _root, _hyp = _identified_case()
    fix = _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    case.evidence.append(cited._row("relief10", cited._GONE, 10, "200s again"))
    case.current_turn = 10
    assert case.progress.outcome is None, "premise: the first record"

    record_case_outcome(case, at=WritePoint.CYCLE)

    window = _record_window(case, fix)
    assert (window.state, window.failure_turn) == ("failed", 9)
    assert _eid("relief10") not in window.problem_row_ids
    assert window.problem_row_ids, "premise: the relief the failure read"


def test_a_demotion_names_the_attempt_that_targeted_its_cause():
    """Item 4: two kinded fixes in one turn, A's first: B's demotion is filed
    under B's fix."""
    case, b, hyp_b, rows = cited._kinded_a_fell_older_b_stands()
    record_case_outcome(case, at=WritePoint.CYCLE)
    cited._fix(case, 10, "remediation", [cited._ROOT_ID], "re-raise max_connections")
    fix_b = cited._fix(case, 10, "remediation", [b.node_id], cited._FIX)
    cited._recheck(case, rows[0].evidence_id, turn=11, label="rc_b")
    _present(case, 12, "back5")

    _cycle(case, 12)

    assert hyp_b.state == HypothesisState.REFUTED
    (demotion,) = case.progress.outcome.demotions
    assert demotion.action_id == fix_b.action_id


def test_a_users_refutation_on_the_failed_fix_records_the_demotion():
    """Item 5: the user refutes B in the failing turn, before the cycle."""
    case, _b, hyp_b, fix, rc_b = _failed_on_a()
    _cycle(case, 13)
    _present(case, 14, "back3")
    case.current_turn = 14
    intent = {"hypothesis_id": hyp_b.hypothesis_id, "action": "refute"}

    _apply_hypothesis_action_intent(HypothesisManager(), case, intent, "no", {})

    assert hyp_b.state == HypothesisState.REFUTED
    (demotion,) = case.progress.outcome.demotions
    assert (demotion.action_id, demotion.cause_row_id) == (
        fix.action_id,
        rc_b.evidence_id,
    )


@pytest.mark.parametrize("pre_chain", [False, True], ids=["cycle", "pre-chain"])
@pytest.mark.parametrize("order", ["a_first", "b_first"])
def test_an_unkinded_windows_pair_names_the_row_m6_names(order, pre_chain):
    """Delta F1: an unkinded fix at 10, A's and B's rows re-checked at 13,
    the problem present at 14. The record's pair, M6's attribution and the
    demotion name one row, B's, whichever was recorded first, and whether
    the pair is minted before M6 (the mint before the chain emission) or
    after it."""
    case, _b, hyp_b, rows = cited._kinded_a_fell_older_b_stands()
    record_case_outcome(case, at=WritePoint.CYCLE)
    fix = cited._fix(case, 10, None, [], cited._FIX)
    labels = [(_eid("pool_log"), "rc_a3"), (rows[0].evidence_id, "rc_b")]
    for named, label in labels if order == "a_first" else labels[::-1]:
        cited._recheck(case, named, turn=13, label=label)
    _present(case, 14, "back3")
    case.current_turn = 14
    if pre_chain:
        record_case_outcome(case, at=WritePoint.PRE_CHAIN)

    _cycle(case, 14)

    assert hyp_b.state == HypothesisState.REFUTED
    (demotion,) = case.progress.outcome.demotions
    pair = _record_window(case, fix).disconfirmation
    assert pair.cause_row_id == demotion.cause_row_id == _eid("rc_b")
