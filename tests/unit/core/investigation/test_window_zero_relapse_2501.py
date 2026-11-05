"""#2501: window 0 fails on a relapse (ruling (a), 2026-11-04), and #2484's
steer for a fix done outside the case.

With no dated fix every observation sits in window 0. Once the identified
cause was shown removed (after it was first posited) and then the problem
shown gone, the problem seen present again is a relapse: window 0 FAILS, readiness
asks for a new fix, and no resolve offer opens. Persistence with no earlier
relief still fails nothing (window 0's diagnosis-time reading), and window 0
never carries a disconfirmation, so M6 acts on nothing.

Real turns through ``_apply_investigation_updates`` (``_record``), and
``MilestoneEngine.process_turn`` where an opener is read. No live LLM.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    _fix_failure_refusal,
)
from faultmaven.core.investigation.cause_assurance import (
    cause_recheck_status,
    fix_windows,
    latest_decided_fix_window,
)
from faultmaven.core.investigation.milestone_engine.chain_emission import (
    _apply_chain_emission,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.offer_projection import project_offers
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CausalNode,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InvestigationActionType,
    MitigationRecord,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProposedAction,
    ValidationMethod,
)
from tests.case_builders import inquiry_case, with_declined_offer
from tests.unit.core.investigation.bound_rows import VERIFYING_ID
from tests.unit.core.investigation.fix_turns_2039 import _engine_rows
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.test_fix_registration_lag_2427 import (
    _PREVIOUS,
    _accepted,
    _ev,
    _fix,
    _identified,
    _registered,
    _reported,
    _stamps,
    _turn,
)
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSE_GONE,
    _GONE,
    _PRESENT,
    _SUPPORT,
    _obs,
    _record,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _accept,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine
from tests.unit.core.investigation.test_resolution_backstop_turn import _make_repo

pytestmark = pytest.mark.unit

_NEW_FIX = "a new fix after the one that did not hold"


def _cause_gone(summary="max_connections reads 100 after the change"):
    return _obs(_CAUSE_GONE, summary, _SUPPORT)


def _gone(summary="checkout returns 200, 503 rate 0.0/s"):
    return _obs(_GONE, summary, VERIFYING_ID)


def _back(summary="checkout returns 503 again"):
    return _obs(_PRESENT, summary, VERIFYING_ID)


def _window_zero(case):
    """(state, relief turn, failure turn, disconfirmation) of window 0."""
    window = fix_windows(case)[0]
    return window.state, window.relief_turn, window.failure_turn, window.disconfirmation


async def _record_ready(case=None):
    """The #1927 case identified, no fix dated; at 5 the cause re-checked gone
    and the problem gone: READY on window 0."""
    if case is None:
        case, *_ = _identified_case()
    eng = _engine()
    await _record(eng, case, 5, _cause_gone(), _gone())
    assert [(w.fix_turn, w.state) for w in fix_windows(case)] == [(None, "succeeded")]
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    return eng, case


async def _relapsed(case=None):
    """#2501's reproduction: ``_record_ready``, then the problem back at 6."""
    eng, case = await _record_ready(case)
    await _record(eng, case, 6, _back())
    return eng, case


async def _silent_turn(case, turn: int, message: str, **process) -> dict:
    """Turn ``turn`` through ``process_turn``, the model proposing nothing."""
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates={}
        )
    )
    case.current_turn = turn
    return await engine.process_turn(
        case=case, user_message=message, redaction_ctx=None, **process
    )


# ---------------------------------------------------------------------------
# 1-2. The relapse fails window 0, the failure stays, and nothing offers the
# resolution.
# ---------------------------------------------------------------------------


async def test_1_a_relapse_after_an_out_of_band_fix_fails_window_0():
    """Window 0 FAILED at 6 on the relief at 5; readiness asks for a new fix,
    the backstop offers nothing, and M6 demotes nothing: a relapse."""
    case, root, hyp = _identified_case()
    _, case = await _relapsed(case=case)

    assert _window_zero(case) == ("failed", 5, 6, None)
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert readiness.missing[0] == _NEW_FIX
    assert readiness.message.startswith("The last fix didn't hold")
    assert _fix_failure_refusal(case, latest_decided_fix_window(case)) == "relapse"
    assert hyp.state == HypothesisState.VALIDATED
    assert root.node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []

    await _silent_turn(case, 7, "what now?")
    assert case.offer_record.open is None


async def test_1_a_later_attach_of_the_root_keeps_window_0_failed():
    """The failure is final: after the relapse the identified root is handed to
    a new hypothesis through chain emission (its owner moves to another
    chain), which re-stamps the root's attach turn. A cause and problem
    re-check at 9 then confirm nothing: window 0 stays FAILED, readiness asks
    for a new fix, and no resolve offer opens."""
    case, root, owner = _identified_case()
    # As the root's first attach through chain emission records it.
    root.metadata["root_first_attached_turn"] = 1
    eng, case = await _relapsed(case=case)
    heir = Hypothesis(
        statement="the postgres connection limit starves the checkout pool",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="re-asserted after the relapse",
        generated_at_turn=8,
    )
    case.hypotheses[heir.hypothesis_id] = heir
    case.current_turn = 8
    elsewhere = SimpleNamespace(
        statement="pgbouncer runs in session mode",
        node_type=NodeType.ROOT,
        produces="D",
        and_group=None,
    )
    _apply_chain_emission(
        case,
        SimpleNamespace(
            causal_nodes_to_add=[elsewhere],
            causal_edges_to_add=[],
            node_evidence_links=[],
        ),
        {
            "hypotheses_generated": [heir.hypothesis_id],
            "hyp_root_refs": {
                owner.hypothesis_id: "new_index_0",
                heir.hypothesis_id: root.node_id,
            },
        },
    )
    assert heir.root_node_id == root.node_id, "premise: handed off"
    assert root.root_attached_turn == 8, "premise: re-stamped"

    await _record(eng, case, 9, _cause_gone("max_connections reads 100"), _gone())

    assert _window_zero(case)[:3] == ("failed", 5, 6)
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert readiness.missing[0] == _NEW_FIX
    await _silent_turn(case, 10, "is it resolved?")
    assert case.offer_record.open is None


async def test_2_under_a_decline_no_reopen_chip_and_its_click_opens_nothing():
    """#2501's Evidence: a standing resolve decline, then the relapse. The
    "Mark it resolved" chip is no longer built, and a click on the one shown
    before the relapse is refused, since the decline no longer covers a
    resolvable case (the engine's guard here; the service boundary answers
    422): no resolve offer opens."""
    eng, case = await _record_ready()
    with_declined_offer(case, kind="resolve", basis="backstop")
    chip = project_offers(case).reopen_chip
    assert chip is not None, "premise: the decline's chips are built"

    await _record(eng, case, 6, _back())

    assert _window_zero(case)[0] == "failed"
    assert project_offers(case).reopen_chip is None
    with pytest.raises(ValueError, match="not a user-selectable case action"):
        await _silent_turn(
            case,
            7,
            "Mark this case resolved.",
            intent_type="status_transition",
            intent_data=dict(chip["intent"]),
        )
    assert case.offer_record.open is None


# ---------------------------------------------------------------------------
# 3. What fails nothing: window 0's diagnosis-time reading. Its first case is
# the shape of plan test 4: no disconfirmation, so M6 demotes nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "turns",
    [
        # A cause row, then the problem present, never gone.
        [(5, _cause_gone), (6, _back)],
        # The problem present before any relief.
        [(5, _cause_gone), (6, _back), (7, _gone)],
        # The relief precedes the cause re-check (the anchor).
        [(6, _gone), (7, _cause_gone), (8, _back)],
    ],
    ids=["no-relief", "present-before-relief", "relief-before-anchor"],
)
async def test_3_persistence_with_no_relief_after_the_anchor_fails_nothing(turns):
    """No disconfirmation either, so M6 demotes nothing (test 4)."""
    case, root, hyp = _identified_case()
    eng = _engine()
    for turn, row in turns:
        await _record(eng, case, turn, row())

    state, _, failure_turn, disconfirmation = _window_zero(case)
    assert state != "failed"
    assert (failure_turn, disconfirmation) == (None, None)
    assert hyp.state == HypothesisState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []
    if turns[0][1] is _gone:
        # Not READY on its legs either: no problem-gone row after the cause.
        verdict = assess_resolution_readiness(case).verdict
        assert verdict == ResolutionReadiness.NEEDS_INFO


async def test_3_a_fallen_candidates_exclusion_anchors_nothing():
    """A causal_absence row bound only through the window pool (a fallen root
    carrying M6's mark), then the problem gone, then present: the row is no
    removal of the identified cause, so window 0 does not fail."""
    case, *_ = _identified_case()
    fallen_support = Evidence(
        evidence_id="ev_0000fa11e501",
        summary="pgbouncer pool_size is 20",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=case.evidence[0].source_file_id,
        collected_by="llm",
        collected_at_turn=2,
    )
    marker = Evidence(
        evidence_id="ev_0000fa11e502",
        summary="engine: a fix of the pool size did not hold",
        primary_purpose="disconfirmation",
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        source_type=EvidenceSourceType.ENGINE_INFERENCE,
        collected_by="engine",
        collected_at_turn=2,
    )
    case.evidence += [fallen_support, marker]
    case.causal_nodes["cn_0000fa11e500"] = CausalNode(
        node_id="cn_0000fa11e500",
        statement="the pgbouncer pool is too small",
        node_type=NodeType.ROOT,
        node_state=NodeState.REFUTED,
        refutation_reason="the pool fix did not hold",
        validation_method=ValidationMethod.EMPIRICAL,
        belief=0.1,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=evidence_id,
                stance=stance,
                reasoning="r",
                linked_at_turn=2,
            )
            for evidence_id, stance in (
                (fallen_support.evidence_id, EvidenceStance.SUPPORTS),
                (marker.evidence_id, EvidenceStance.REFUTES),
            )
        ],
        generated_at_turn=1,
    )
    eng = _engine()
    await _record(
        eng, case, 5, _obs(_CAUSE_GONE, "pool_size reads 50", "ev_0000fa11e501")
    )
    await _record(eng, case, 6, _gone())
    await _record(eng, case, 7, _back())

    window = fix_windows(case)[0]
    assert window.cause_rows and not window.confirmed_cause_rows, "premise"
    assert window.state != "failed"


@pytest.mark.parametrize("restart_first", [True, False])
async def test_3_a_row_from_before_the_cause_was_posited_anchors_nothing(
    restart_first,
):
    """The critique's sequence, no fix ever done: a restart shows the problem
    gone at 2; at 3 a causal_absence row names X's support ("pool size reads
    100, not it"); the problem is back at 4; X is identified at 7. The row at
    3 predates X's positing and the relief at 2 precedes any anchor. With the
    row first and the restart after it, the positing floor alone holds."""
    case, *_ = _identified_case(hypothesis_turn=7)
    eng = _engine()
    rows = [
        _gone("checkout returns 200 after the restart"),
        _cause_gone("pool size reads 100, not it"),
    ]
    for turn, row in zip((2, 3), rows if restart_first else rows[::-1]):
        await _record(eng, case, turn, row)
    await _record(eng, case, 4, _back())
    await _silent_turn(case, 7, "what next?")

    assert fix_windows(case)[0].cause_rows, "premise: the row binds to X"
    assert _window_zero(case)[0] != "failed"


async def test_3_a_turn_holding_gone_and_present_after_the_relief_reads_gone():
    """The relief at 5, then a turn holding both a problem-gone and a
    problem-present row: it reads as gone, as in a dated window."""
    eng, case = await _record_ready()

    await _record(eng, case, 6, _back(), _gone("checkout is fine again"))

    assert _window_zero(case) == ("succeeded", 5, None, None)


async def test_3_a_fallen_causes_pre_posit_row_anchors_nothing():
    """A "not it" row at 3 naming the support of a cause first posited at 7, a
    restart's relief at 4, the problem back at 6; then the cause falls, so the
    confirmation pool is its fallen root and no hypothesis stands. The row
    still predates that cause's first posit: window 0 does not fail."""
    case, root, hyp = _identified_case(hypothesis_turn=7)
    eng = _engine()
    await _record(eng, case, 3, _cause_gone("pool size reads 100, not it"))
    await _record(eng, case, 4, _gone("checkout returns 200 after the restart"))
    await _record(eng, case, 6, _back())
    hyp.state = HypothesisState.REFUTED
    root.node_state = NodeState.REFUTED
    case.progress.identified_cause_root_ids = [root.node_id]

    assert fix_windows(case)[0].confirmed_cause_rows, "premise: the row binds"
    assert _window_zero(case)[0] != "failed"


# ---------------------------------------------------------------------------
# 5-6. The inclusive end, and dating a re-fix after the relapse.
# ---------------------------------------------------------------------------


async def test_5_the_problem_back_in_the_next_fixs_execution_turn_is_a_relapse():
    """Window 0 ends at the next dated fix's execution turn, inclusive for
    persistence: the problem present in that turn, after window 0's relief,
    is window 0's relapse, not the new fix's failure."""
    eng, case = await _record_ready()
    _accept(case, InvestigationActionType.SOLUTION, 8)

    await _record(eng, case, 8, _back())

    assert [(w.fix_turn, w.state, w.failure_turn) for w in fix_windows(case)] == [
        (None, "failed", 8),
        (8, "unknown", None),
    ]


async def test_6_a_re_fix_dated_to_its_report_keeps_window_0s_failure():
    """The relapse at 6; at 8 the user reports a re-fix with its output; at 9
    it is registered, declared reported at 8. No bound refuses it, window 0
    keeps its failure (its rows are all before 8), and the new window
    confirms."""
    eng, case = await _relapsed()
    await _record(eng, case, 8, _cause_gone("max_connections reads 200"), _gone())

    await _record(
        eng,
        case,
        9,
        _cause_gone("max_connections still reads 200"),
        solutions_to_add=[_fix()],
        milestones=_accepted(_PREVIOUS),
    )

    assert _stamps(case) == [(8, 9)]
    assert _window_zero(case) == ("failed", 5, 6, None)
    assert [(w.fix_turn, w.state) for w in fix_windows(case)][1:] == [(8, "succeeded")]
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


# ---------------------------------------------------------------------------
# Readers of a failed window 0: the failure cycle and M6's refusal label.
# ---------------------------------------------------------------------------


async def test_a_late_registered_fix_counts_as_one_failed_attempt():
    """#2427's undeclared lag: the fix reported at 10, registered at 11 beside
    a 503 paste (window 0's relapse), 503 again at 13 (the fix's own failure).
    One recorded attempt failed, so no fix-failure cycle."""
    eng, case, _hyp = _identified()
    await _reported(eng, case)
    await _registered(eng, case, None, _ev(_PRESENT, "checkout returns 503 again"))
    await _turn(eng, case, 13, _ev(_PRESENT, "checkout still returns 503"))

    assert [(w.fix_turn, w.state, w.failure_turn) for w in fix_windows(case)] == [
        (None, "failed", 11),
        (11, "failed", 13),
    ], "premise"
    assert ProgressMonitor()._detect_fix_failure_cycle(case, "solution") is False


@pytest.mark.parametrize("undatable", [False, True])
async def test_a_relapse_keeps_the_undatable_acceptance_label(undatable):
    """With a SOLUTION accepted but undated, every row is in window 0, and its
    relapse keeps M6's ``undatable_acceptance`` label (a transition series kept
    visible); without one, it reads ``relapse``."""
    case, *_ = _identified_case()
    if undatable:
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="raise max_connections to 100",
                proposed_in_turn=3,
                state="accepted",
            )
        )
    _, case = await _relapsed(case=case)

    assert _window_zero(case)[0] == "failed"
    label = "undatable_acceptance" if undatable else "relapse"
    assert _fix_failure_refusal(case, latest_decided_fix_window(case)) == label


# ---------------------------------------------------------------------------
# 7. The notices name the fix done outside the case, never "turn None".
# ---------------------------------------------------------------------------


async def test_7_the_failed_fix_notice_names_the_fix_done_outside_the_case():
    eng, case = await _relapsed()

    _, meta = await _record(eng, case, 7, _gone("checkout is fine now"))

    feedback = meta.get("system_feedback") or ""
    assert (
        "SYSTEM: The fix done outside the case did not hold: the problem was "
        "seen gone at turn 5, then seen again at turn 6." in feedback
    )
    assert "None" not in feedback


@pytest.mark.parametrize("newer", [False, True])
async def test_7_the_recheck_reason_names_the_fix_done_outside_the_case(newer):
    """The cause re-check at 5 no longer counts, and says why."""
    _, case = await _relapsed()
    then = "only a new fix can confirm"
    if newer:
        _accept(case, InvestigationActionType.SOLUTION, 8)
        then = "the newer fix (executed turn 8) is not re-checked yet"

    assert [why for _, why in cause_recheck_status(case)] == [
        f"the fix done outside the case was seen failing at turn 6; {then}"
    ]


# ---------------------------------------------------------------------------
# 8. The prompt (#2484).
# ---------------------------------------------------------------------------

_RECHECKS_SCOPE = (
    "After a fix, one recorded here or one the user reports carrying out "
    "outside the case, each absence row names in `rechecks` the row it re-checks"
)
_PRESENT_RECHECKS = (
    "a symptom_evidence row showing the problem still present or back names "
    "the row that showed the problem"
)
_REGISTER = (
    "A fix the user reports carrying out outside the case is registered in the "
    "same response, when the root cause stands established: propose it as a "
    "SolutionToAdd and set solution_accepted with accepted_action_ref "
    "new_index_N naming it"
)
_MITIGATION = (
    "A restart, a failover or another workaround is a mitigation, recorded as "
    "one (symptom_absence only), never registered as a SOLUTION."
)
_SCOPE = (
    "After a fix, one recorded here or one the user reports carrying out "
    "outside the case"
)
_UNPROPOSED = (
    "or, for a fix the user carried out that was never proposed and only when "
    "the root cause stands established, to new_index_N naming it in "
    "solutions_to_add"
)
_UNPROPOSED_MITIGATION = (
    "A restart, a failover or another workaround the user carried out is a "
    "mitigation, never registered as a SOLUTION."
)


def _stage_case(stage: str):
    if stage == "inquiry":
        return inquiry_case(statement="checkout returns 503")
    case, *_ = _identified_case()
    if stage == "treatment":
        _accept(case, InvestigationActionType.SOLUTION, 4)
        case.progress.solution_accepted = True
    elif stage == "mitigation":
        case.progress.mitigation = MitigationRecord(proposed_at_turn=2, accepted=True)
    return case


@pytest.mark.parametrize("stage", ["diagnosis", "mitigation", "treatment", "inquiry"])
def test_8_the_tree_steers_an_out_of_band_fix_on_every_investigating_stage(stage):
    """Every investigating stage renders the steer, DIAGNOSIS with no fix on
    record included; its own text scopes it to rows after a fix, so a
    diagnosis-time exclusion is not steered to bind. INQUIRY renders none."""
    case = _stage_case(stage)
    prompt = " ".join(
        get_prompt_for_case(
            case, "I fixed it myself", given_tools=DEFAULT_GIVEN
        ).split()
    )
    investigating = stage != "inquiry"
    if investigating:
        assert case.current_stage.value == stage, "premise"
    for text in (_SCOPE, _RECHECKS_SCOPE, _PRESENT_RECHECKS, _REGISTER, _MITIGATION):
        assert (text in prompt) is investigating
    for text in (_UNPROPOSED, _UNPROPOSED_MITIGATION):
        assert (text in prompt) is (stage == "diagnosis")
