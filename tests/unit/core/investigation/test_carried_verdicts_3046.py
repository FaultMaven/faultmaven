"""The deciders read carried verdicts (#3046 S3a-2, stage S3 of #2615).

A fix window the outcome record holds FAILED reads FAILED, with its pair, to
every decider (``carried_fix_windows``): across a re-root, a pool move and a
re-read, until a new fix opens a new window. The record's sections read the
same windows; a later disconfirmation of a FAILED window reaches the record,
counted under its own label (#2728); and a window a decider read FAILED in a
cycle is in that cycle's record (the same-cycle mint, #2582).

Each regression is the issue's own sequence, with the fresh read's reversal
as a premise, so it fails on ``main``. No live LLM.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_SUCCEEDED,
    _no_confirming_window,
    carried_fix_windows,
    fix_windows,
)
from faultmaven.core.investigation.milestone_engine import stage_gates
from faultmaven.core.investigation.narration.codes import DECISIONS_KEY
from faultmaven.core.investigation.outcome import (
    compute_case_outcome,
    narration_outcome,
    record_case_outcome,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    EvidenceToAdd,
    HypothesisUpdate,
    InvestigationResponse_Treatment,
    NodeEvidenceLinkToAdd,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    EvidenceSourceType,
    FixWindowState,
    HypothesisState,
    InvestigationActionType,
    NoWindowKind,
    ResolutionVerdict,
    WritePoint,
)
from tests.unit.core.investigation import test_cause_leg_cited_basis_2557 as cited
from tests.unit.core.investigation.bound_rows import VERIFYING_ID, _row, upload
from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
    _cited_but_unlinked,
    _deepen,
    _fix,
    _relapse,
    _window,
    conflicts,  # noqa: F401 — the fixture
)
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _engine as _turn_engine,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _accept,
    _cause_gone,
    _eid,
    _engine_rows,
    _identified_case,
    _problem_gone,
    _recompute_at,
    _still_failing,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _row as _row_1927,
)

pytestmark = pytest.mark.unit

_C = EvidenceCategory
_NEEDS_INFO = ResolutionReadiness.NEEDS_INFO


def _cycle(case, turn) -> None:
    """A cycle's end: the recompute, then the record (#2636)."""
    _recompute_at(case, turn)
    record_case_outcome(case, at=WritePoint.CYCLE)


def _deeper_recheck_and_relief(case, *, gone=11, relief=12) -> None:
    """The deeper cause's datum re-checked gone, then the problem gone."""
    _row(
        case,
        _C.CAUSAL_ABSENCE_EVIDENCE,
        turn=gone,
        evidence_id=_eid("a2gone"),
        summary="pgbouncer pool_size now reads 100",
        rechecks=_eid("deeper"),
    )
    _row(
        case,
        _C.SYMPTOM_ABSENCE_EVIDENCE,
        turn=relief,
        evidence_id=_eid("relief2"),
        summary="checkout answers 200",
        rechecks=VERIFYING_ID,
    )


# ---------------------------------------------------------------------------
# #2595: an unkinded window read FAILED is not re-bound to the pool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kinded", [False, True], ids=["unkinded", "kinded-twin"])
def test_2595_a_failed_fix_confirms_nothing_after_the_cause_deepens(kinded):
    """#2595's first sequence: the fix of A relapses (FAILED), A deepens to
    A2, and A2's datum is re-checked gone with the problem gone. The kinded
    twin stays FAILED on the fresh read too, as on ``main``."""
    case, root, hyp = _identified_case()
    kind = dict(target_ids=[root.node_id], fix_kind="remediation") if kinded else {}
    fix = _fix(case, **kind)
    _relapse(case, rechecks=_eid("pool_log"))
    _cycle(case, 9)
    _deepen(case, hyp, root)
    record_case_outcome(case, at=WritePoint.CYCLE)
    _deeper_recheck_and_relief(case)
    _recompute_at(case, 12)

    fresh = FIX_FAILED if kinded else FIX_SUCCEEDED
    assert fix_windows(case)[-1].state == fresh, "premise: the fresh read"
    assert carried_fix_windows(case)[-1].state == FIX_FAILED
    assert assess_resolution_readiness(case).verdict == _NEEDS_INFO
    record_case_outcome(case, at=WritePoint.CYCLE)
    record = case.progress.outcome
    assert record.resolution.verdict == ResolutionVerdict.NEEDS_INFO
    assert _window(record, fix.action_id).state == FixWindowState.FAILED


def test_2595_a_failure_through_a_cited_row_stays_after_another_cause_stands():
    """#2595's second sequence: a kinded fix of A fails and M6 demotes A; an
    unkinded fix F fails on A's cited, unlinked row; B is posited and its
    cited row re-checked gone with the problem gone. ``main`` read F
    SUCCEEDED and the case READY."""
    case, a, _hyp_a = cited._identified_case()
    why = _cited_but_unlinked(case, a)
    _cycle(case, 5)
    cited._fix(case, 6, "remediation", [a.node_id], cited._FIX)
    cited._recheck(case, _eid("pool_log"), turn=7, label="rc_a")
    case.evidence.append(cited._row("back", cited._PRESENT, 8, "503s again"))
    _cycle(case, 8)
    f = cited._fix(case, 9, None, [], "restart checkout with a bigger pool")
    cited._recheck(case, why.evidence_id, turn=10, label="rc_a2")
    case.evidence.append(cited._row("back2", cited._PRESENT, 11, "503s again"))
    _cycle(case, 11)
    assert fix_windows(case)[-1].state == FIX_FAILED, "premise: F failed"
    _b, _hyp_b, rows = cited._stands(
        case,
        "cn_00000000256b",
        "a memory leak in the gunicorn worker",
        cites=None,
        turn=12,
    )
    _cycle(case, 12)
    cited._recheck(case, rows[0].evidence_id, turn=13, label="rc_b")
    case.evidence.append(cited._row("gone", cited._GONE, 14, "503s at 0.0/s"))
    _recompute_at(case, 14)

    assert fix_windows(case)[-1].state == FIX_SUCCEEDED, "premise: re-bound"
    assert carried_fix_windows(case)[-1].state == FIX_FAILED
    assert assess_resolution_readiness(case).verdict == _NEEDS_INFO
    record_case_outcome(case, at=WritePoint.CYCLE)
    assert case.progress.outcome.resolution.verdict == ResolutionVerdict.NEEDS_INFO
    assert _window(case.progress.outcome, f.action_id).failure_turn == 11


# ---------------------------------------------------------------------------
# #2582: a failure read through a cited row survives the re-root
# ---------------------------------------------------------------------------


def test_2582_window_zero_read_through_a_cited_row_stays_failed_after_a_re_root():
    """The window-0 sequence: a relapse read through the root's cited,
    unlinked row, then a re-root (which erases the citation), the deeper
    root's datum re-checked and the problem gone. The carried window keeps
    ``observed`` (amendment 1): a FAILED window is observed, so the reason no
    window confirms is the failure, with its turns."""
    case, root, hyp = _identified_case()
    row = _cited_but_unlinked(case, root)
    _recompute_at(case, 5)
    _relapse(case, rechecks=row.evidence_id)
    _cycle(case, 9)
    _deepen(case, hyp, root)
    assert fix_windows(case)[0].state != FIX_FAILED, "premise: re-read"

    zero = carried_fix_windows(case)[0]
    assert (zero.state, zero.observed, zero.relief_turn, zero.failure_turn) == (
        FIX_FAILED,
        True,
        8,
        9,
    )
    why = _no_confirming_window(case, carried_fix_windows(case))
    assert (why.kind, why.failure_turn) == (NoWindowKind.FAILED, 9)
    _deeper_recheck_and_relief(case)
    _recompute_at(case, 12)
    assert assess_resolution_readiness(case).verdict == _NEEDS_INFO


def _obs(category, summary, file_id, rechecks=None) -> EvidenceToAdd:
    return EvidenceToAdd(
        summary=summary,
        extract=summary,
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=file_id,
        rechecks=rechecks,
    )


async def test_2582_a_failure_and_a_deepening_in_one_turn_reach_its_record():
    """The same cycle (ruling 4): turn 8 carries "still failing" and the
    deepening. The gates read the unkinded fix FAILED, and the emission's
    re-root reverses the fresh read before the cycle's record. The mint
    before the emission keeps it, with its pair. M6, after the chain, reads
    the carried pair with the fresh rows: none addresses the deeper root,
    so nothing is demoted, as on ``main``."""
    case, root, hyp = _identified_case()
    fix = _fix(case)
    _row(
        case,
        _C.CAUSAL_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id=_eid("gone"),
        summary="max_connections now reads 100",
        rechecks=_eid("pool_log"),
    )
    _cycle(case, 7)
    file_id = upload(case, 8)
    deeper = "pgbouncer caps the pool at 20 connections"
    engine = _turn_engine()
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Treatment(
            agent_response="Noted.",
            state_updates=InvestigationResponse_Treatment.TreatmentStateUpdate(
                evidence_to_add=[
                    _obs(
                        _C.SYMPTOM_EVIDENCE,
                        "checkout returns 503 still",
                        file_id,
                        VERIFYING_ID,
                    ),
                    _obs(_C.CAUSAL_EVIDENCE, "pgbouncer.ini pool_size = 20", file_id),
                    _obs(
                        _C.CAUSAL_EVIDENCE, "pgbouncer holds 20 server conns", file_id
                    ),
                ],
                causal_nodes_to_add=[
                    CausalNodeToAdd(
                        statement=deeper, node_type="root", produces=root.node_id
                    )
                ],
                node_evidence_links=[
                    NodeEvidenceLinkToAdd(
                        node_ref="new_index_0",
                        evidence_id_ref=f"new_index_{i}",
                        stance="supports",
                        reasoning="bears on the deeper root",
                    )
                    for i in (1, 2)
                ],
                hypotheses_to_update=[
                    HypothesisUpdate(
                        hypothesis_id=hyp.hypothesis_id,
                        root_node_ref="new_index_0",
                        root_is_terminal=True,
                        terminal_basis_evidence_ids=["new_index_1"],
                    )
                ],
            ),
        )
    )
    case.current_turn = 8
    await engine.process_turn(
        case=case, user_message="still 503; pgbouncer caps it", redaction_ctx=None
    )

    assert hyp.root_node_id != root.node_id, "premise: deepened"
    assert fix_windows(case)[-1].state != FIX_FAILED, "premise: re-read"
    window = _window(case.progress.outcome, fix.action_id)
    pair = window.disconfirmation
    assert (window.state, window.failure_turn) == (FixWindowState.FAILED, 8)
    assert (pair.cause_turn, pair.problem_turn) == (7, 8)
    assert carried_fix_windows(case)[-1].disconfirmation == (7, 8)
    assert hyp.state != HypothesisState.REFUTED and _engine_rows(case) == []


# ---------------------------------------------------------------------------
# #2728: a later disconfirmation reaches the record, counted as an addition
# ---------------------------------------------------------------------------


def _relapsed_at_12():
    """#2728's case: fix at 6, the cause and the problem gone at 7, the
    problem back at 12 (FAILED, no pair)."""
    case, _root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 6)
    case.evidence += [_cause_gone(7), _problem_gone(7, label="relief_7")]
    _cycle(case, 7)
    case.evidence += [_still_failing(12, label="back_at_12")]
    _cycle(case, 12)
    (attempt,) = case.progress.outcome.attempts
    assert attempt.window.disconfirmation is None, "premise: no pair"
    case.evidence += [
        _row_1927(
            "cause_still_gone",
            _C.CAUSAL_ABSENCE_EVIDENCE,
            14,
            "max_connections still reads 100",
        ),
        _still_failing(14, label="still_14"),
    ]
    return case, hyp, attempt.action_id


def test_2728_the_record_takes_the_later_pair_once_and_counts_it_apart(
    conflicts,
):  # noqa: F811
    case, hyp, action_id = _relapsed_at_12()
    _recompute_at(case, 14)
    assert hyp.state == HypothesisState.REFUTED, "M6 reads the fresh pair, as on main"
    record_case_outcome(case, at=WritePoint.CYCLE)
    record_case_outcome(case, at=WritePoint.CYCLE)

    window = _window(case.progress.outcome, action_id)
    pair = window.disconfirmation
    assert (window.state, window.relief_turn, window.failure_turn) == ("failed", 7, 12)
    assert (pair.cause_turn, pair.problem_turn) == (14, 14)
    assert conflicts == ["attempts.window.disconfirmation"]


@pytest.mark.parametrize(
    ("at", "taken"),
    [
        ("narration", False),
        (WritePoint.COMMIT, False),
        (WritePoint.CYCLE, True),
        (WritePoint.REPLAY, True),
    ],
)
def test_2728_the_pair_is_taken_only_at_a_minting_point(at, taken):
    """Amendment 6: an unstored narration fold or a save's fold shows no pair
    the saved record lacks."""
    case, _hyp, action_id = _relapsed_at_12()
    case.current_turn = 14
    if at == "narration":
        record = narration_outcome(case)
    else:
        record_case_outcome(case, at=at)
        record = case.progress.outcome
    pair = _window(record, action_id).disconfirmation
    assert (pair is not None) is taken
    assert ("attempts.window.disconfirmation" in str(record.disagreements)) is taken


# ---------------------------------------------------------------------------
# The overlay rule's edges
# ---------------------------------------------------------------------------


def test_an_unkinded_failed_window_ends_at_the_attempt_after_its_failure():
    """Amendment 3: an unkinded fix at 6 relapses at 9, an interception runs
    at 10, the cause deepens at 11. The carried window ends at 10 and holds
    no re-check after it, so the deeper cause's re-check at 12 is no row of
    the failed fix."""
    case, root, hyp = _identified_case()
    _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    _fix(case, turn=10, fix_kind="interception", target_ids=[root.node_id])
    case.proposed_actions[-1].description = "restart the pod"
    _cycle(case, 10)
    _deepen(case, hyp, root, turn=11)
    _deeper_recheck_and_relief(case, gone=12, relief=13)
    _recompute_at(case, 13)

    fresh = fix_windows(case)[-1]
    assert (fresh.end_turn, fresh.state) == (None, FIX_SUCCEEDED), "premise"
    window = carried_fix_windows(case)[-1]
    assert (window.end_turn, window.state, window.failure_turn) == (10, FIX_FAILED, 9)
    assert _eid("a2gone") not in {e.evidence_id for e in window.cause_rows}
    assert assess_resolution_readiness(case).verdict == _NEEDS_INFO


def test_a_kinded_failed_windows_span_is_the_fresh_reads():
    """A second fix at 11 ends the carried window where the fresh read ends
    it."""
    case, root, _hyp = _identified_case()
    _fix(case, target_ids=[root.node_id], fix_kind="remediation")
    _relapse(case, rechecks=_eid("pool_log"))
    _cycle(case, 9)
    _fix(case, turn=11, target_ids=[root.node_id], fix_kind="remediation")
    _recompute_at(case, 11)
    window = carried_fix_windows(case)[1]
    assert (window.fix_turn, window.end_turn, window.state) == (6, 11, FIX_FAILED)
    assert window.end_turn == fix_windows(case)[1].end_turn


def test_an_orphan_is_not_inserted():
    """A final FAILED window whose attempt no longer reads stays on the
    record, and the deciders' view is the fresh read's, window for window."""
    case, _root, _hyp = _identified_case()
    fix = _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _cycle(case, 9)
    case.proposed_actions.remove(fix)
    record_case_outcome(case, at=WritePoint.CYCLE)
    assert _window(case.progress.outcome, fix.action_id).state == "failed"
    assert carried_fix_windows(case) == fix_windows(case)


def test_a_re_dated_attempt_is_matched_by_its_action():
    """The attempt's execution turn moves after its window was minted (the
    record keeps the old turn): the verdict follows the action, read at its
    current turn."""
    case, root, hyp = _identified_case()
    fix = _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _cycle(case, 9)
    _deepen(case, hyp, root)
    fix.accepted_in_turn = 7
    assert fix_windows(case)[-1].state != FIX_FAILED, "premise: re-read"
    assert all(w.fix_turn != 6 for w in carried_fix_windows(case))
    window = carried_fix_windows(case)[-1]
    assert (window.fix_turn, window.state, window.failure_turn) == (7, FIX_FAILED, 9)


def test_the_report_dating_guard_still_reads_the_fresh_pair():
    """Amendment 2: the record holds the pair (14, 14); dating a fix
    registered at 15 to 14 would move that row out of the window it was
    read in, so the declaration is ignored. Read off the record's pair
    both times, the guard could never fire."""
    case, _hyp, _action_id = _relapsed_at_12()
    _cycle(case, 14)
    case.current_turn = 15
    _accept(case, InvestigationActionType.SOLUTION, 15)
    target = case.proposed_actions[-1]
    assert carried_fix_windows(case)[-2].disconfirmation == (14, 14), "premise"
    metadata: dict = {}

    stage_gates._date_to_the_report(case, target, metadata)

    assert target.accepted_in_turn == 15
    (entry,) = metadata[DECISIONS_KEY]
    assert entry["params"]["reason"] == "decides_disconfirmation"


def test_m6_attributes_by_the_fresh_rows_as_on_main():
    """Amendment 4: the rows are not carried (S3b's row rule, #2586). A
    kinded fix of A fails and M6 demotes A; B is identified; B's fix at 10
    fails on B's re-check, with the record written each cycle. M6 demotes B
    at 13 and mints its row, as on ``main``."""
    case, b, hyp_b, rows = cited._kinded_a_fell_older_b_stands()
    record_case_outcome(case, at=WritePoint.CYCLE)
    cited._fix(case, 10, "remediation", [b.node_id], cited._FIX)
    cited._recheck(case, _eid("pool_log"), turn=11, label="rc_a2")
    case.evidence.append(cited._row("back2", cited._PRESENT, 12, "503s again"))
    _cycle(case, 12)
    cited._recheck(case, rows[0].evidence_id, turn=13, label="rc_b")
    _cycle(case, 13)
    assert hyp_b.state == HypothesisState.REFUTED
    assert [e.collected_at_turn for e in _engine_rows(case)] == [8, 13]


# ---------------------------------------------------------------------------
# The record agrees with itself
# ---------------------------------------------------------------------------


def test_the_records_sections_read_the_windows_its_attempts_carry():
    """Amendment 4: folded over a prior the case does not store, every
    section, the readiness it calls included, reads the windows the fold's
    attempts carry, not ``progress.outcome``."""
    case, root, hyp = _identified_case()
    fix = _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    prior = compute_case_outcome(None, case, at=WritePoint.CYCLE)
    _deepen(case, hyp, root)
    _deeper_recheck_and_relief(case)
    _recompute_at(case, 12)
    assert case.progress.outcome is None, "premise: nothing stored"
    assert fix_windows(case)[-1].state == FIX_SUCCEEDED, "premise"

    record = compute_case_outcome(prior, case, at=WritePoint.CYCLE)

    assert _window(record, fix.action_id).state == FixWindowState.FAILED
    assert record.resolution.verdict == ResolutionVerdict.NEEDS_INFO
    assert record.resolution.cause_row_ids == []
    # Outside a computation there is no memo: with nothing stored, the
    # deciders read the fresh read.
    assert carried_fix_windows(case) == fix_windows(case)
