"""Calibration eval for the verification-status join (Phase 0b).

Beyond the clean archetypes, the set includes the three **confusable pairs**
(§5.5) — where identification quality actually lives:
  - data-wall vs model-produced-nothing  (INSUFFICIENT_EVIDENCE vs NOT_YET_PRODUCTIVE)
  - still-working vs walled               (OPEN vs INSUFFICIENT_EVIDENCE)
  - grounded-but-stalled vs not-grounded-stalled (TREATMENT_BLOCKED vs INSUFFICIENT_EVIDENCE)

Deterministic unit tier over ``assess_verification_status`` — model-independent,
which is the whole point: the design disposes any model's output correctly.
"""

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.core.investigation.verification_status import (
    VerificationStatus,
    assess_verification_status,
    work_gate_passed,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NeedObtainability,
    NeedPurpose,
    NeedState,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _nid(seed: int) -> str:
    return f"cn_{seed:012x}"


def _ev(label: str) -> Evidence:
    return Evidence(
        evidence_id=_eid(label),
        summary="an observed fact",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _hyp(category: HypothesisCategory, seed: int) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=f"hyp_{seed:012x}",
        statement=f"hypothesis {seed}",
        category=category,
        state=HypothesisState.CAPTURED,
        rationale="a reason",
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        generated_at_turn=1,
    )


def _problem() -> CausalNode:
    return CausalNode(
        node_id=_nid(0xD),
        statement="D: intermittent latency",
        node_type=NodeType.PROBLEM,
        generated_at_turn=1,
    )


def _grounded_root() -> CausalNode:
    """A VALIDATED ROOT grounded via the deductive arm (validation_method =
    DEDUCTIVE) → ``grade_cause_assurance`` == GROUNDED, no evidence link needed."""
    return CausalNode(
        node_id=_nid(1),
        statement="root cause",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.DEDUCTIVE,
        actionable=True,
        generated_at_turn=1,
    )


def _case(
    *,
    grounded: bool,
    n_hypotheses: int,
    n_categories: int,
    n_evidence: int,
    current_turn: int,
    turns_without_progress: int,
) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=current_turn,
        inquiry=InquiryData(
            proposed_problem_statement="intermittent latency",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="intermittent latency", severity=CaseSeverity.HIGH
        ),
    )
    case.turns_without_progress = turns_without_progress
    case.evidence = [_ev(f"e{i}") for i in range(n_evidence)]

    cats = list(HypothesisCategory)[:n_categories]
    hyps = [_hyp(cats[i % max(len(cats), 1)], i) for i in range(n_hypotheses)]
    case.hypotheses = {h.hypothesis_id: h for h in hyps}

    if grounded:
        d, root = _problem(), _grounded_root()
        case.causal_nodes = {d.node_id: d, root.node_id: root}
        case.causal_edges = [
            CausalEdge(cause_node_id=root.node_id, effect_node_id=d.node_id)
        ]
    return case


# --- work-done shorthand: crosses the ≥2/≥2/≥2 gate --------------------------
def _work_done(**overrides):
    base = dict(
        grounded=False,
        n_hypotheses=3,
        n_categories=2,
        n_evidence=2,
        current_turn=3,
        turns_without_progress=0,
    )
    base.update(overrides)
    return _case(**base)


# --- Archetypes --------------------------------------------------------------


def test_grounded_and_progressing_is_healthy():
    case = _case(
        grounded=True,
        n_hypotheses=2,
        n_categories=2,
        n_evidence=2,
        current_turn=3,
        turns_without_progress=0,
    )
    assert assess_verification_status(case) == VerificationStatus.HEALTHY


def test_work_done_progressing_not_grounded_is_open():
    assert assess_verification_status(_work_done()) == VerificationStatus.OPEN


def test_work_done_stalled_not_grounded_is_insufficient_evidence():
    case = _work_done(current_turn=9, turns_without_progress=5)
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE


# --- Confusable pair 1: data-wall vs model-produced-nothing ------------------
# Both look "stuck". The work gate is the discriminator: a stalled case with no
# real diagnostic work is a provider problem (NOT_YET_PRODUCTIVE), never a
# case-blaming INSUFFICIENT_EVIDENCE verdict.


def test_stalled_but_no_work_is_not_yet_productive_not_insufficient():
    # Silent/empty model: stalled thresholds met, but < the ≥2 work gate.
    empty = _case(
        grounded=False,
        n_hypotheses=0,
        n_categories=0,
        n_evidence=0,
        current_turn=12,
        turns_without_progress=7,
    )
    assert not work_gate_passed(empty)
    assert assess_verification_status(empty) == VerificationStatus.NOT_YET_PRODUCTIVE
    # Its data-wall twin: identical stall, but real work happened.
    walled = _work_done(current_turn=12, turns_without_progress=7)
    assert work_gate_passed(walled)
    assert (
        assess_verification_status(walled) == VerificationStatus.INSUFFICIENT_EVIDENCE
    )


# --- Confusable pair 2: still-working vs walled ------------------------------
# Same work done, same not-grounded — only the progress axis differs.


def test_still_working_vs_walled_split_on_progress_axis():
    still_working = _work_done(current_turn=9, turns_without_progress=2)  # not stalled
    walled = _work_done(current_turn=9, turns_without_progress=5)  # stalled
    assert assess_verification_status(still_working) == VerificationStatus.OPEN
    assert (
        assess_verification_status(walled) == VerificationStatus.INSUFFICIENT_EVIDENCE
    )


# --- Confusable pair 3: grounded-but-stalled vs not-grounded-stalled ---------
# Same stall; the grounding axis splits treatment-blocked from insufficient.


def test_grounded_but_stalled_vs_not_grounded_stalled_split_on_grounding():
    grounded_stalled = _case(
        grounded=True,
        n_hypotheses=2,
        n_categories=2,
        n_evidence=2,
        current_turn=9,
        turns_without_progress=5,
    )
    not_grounded_stalled = _work_done(current_turn=9, turns_without_progress=5)
    assert (
        assess_verification_status(grounded_stalled)
        == VerificationStatus.TREATMENT_BLOCKED
    )
    assert (
        assess_verification_status(not_grounded_stalled)
        == VerificationStatus.INSUFFICIENT_EVIDENCE
    )


# --- Work-gate boundary (fast-exhaustion must not need an arbitrary turn floor
#     beyond the stall thresholds; below-gate is never insufficient) ----------


def test_work_gate_requires_all_three_dimensions():
    assert not work_gate_passed(_work_done(n_hypotheses=1))  # too few hypotheses
    assert not work_gate_passed(_work_done(n_categories=1))  # single category
    assert not work_gate_passed(_work_done(n_evidence=1))  # too little evidence
    assert work_gate_passed(_work_done())  # 3 hyps / 2 cats / 2 evidence


# --- Declared data wall (model-declared obtainability) -----------------------
# The progress axis is stalled by the time thresholds OR by a declared wall:
# every residual candidate's discriminators declared UNOBTAINABLE. A fully
# declared wall reaches INSUFFICIENT_EVIDENCE immediately, without a time-stall.


def _causal_need(case, hyp_id: str, obtainability: NeedObtainability, seed: int):
    return EvidenceNeed(
        need_id=f"eneed_{seed:012x}",
        case_id=case.case_id,
        purpose=NeedPurpose.CAUSAL_VERIFICATION,
        request_text="the discriminating datum",
        rationale="would distinguish the remaining candidates",
        state=NeedState.PENDING,
        obtainability=obtainability,
        motivating_hypothesis_ids=[hyp_id],
        created_at_turn=2,
    )


def _declare(case, per_hyp: dict) -> None:
    """Attach one causal_verification need per hypothesis id → obtainability."""
    case.evidence_needs = [
        _causal_need(case, hyp_id, ob, i)
        for i, (hyp_id, ob) in enumerate(per_hyp.items())
    ]


def test_declared_wall_establishes_stall_without_time_threshold():
    # Not time-stalled (turn 3, twp 0), work-gated, not grounded → OPEN...
    case = _work_done()
    assert assess_verification_status(case) == VerificationStatus.OPEN
    # ...until every residual candidate's discriminator is declared UNOBTAINABLE.
    _declare(case, {h: NeedObtainability.UNOBTAINABLE for h in case.hypotheses.keys()})
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE


def test_candidate_with_no_discriminator_is_not_a_wall():
    # Guard the vacuous ∀-over-empty: a residual candidate with ZERO discriminators
    # is *unknown*, not unresolvable — so leaving one undeclared breaks the wall.
    case = _work_done()
    hyps = list(case.hypotheses.keys())
    _declare(case, {h: NeedObtainability.UNOBTAINABLE for h in hyps[:-1]})
    # hyps[-1] has no need at all → not unresolvable → no wall → still OPEN.
    assert assess_verification_status(case) == VerificationStatus.OPEN


def test_one_obtainable_discriminator_breaks_the_wall():
    case = _work_done()
    hyps = list(case.hypotheses.keys())
    per = {h: NeedObtainability.UNOBTAINABLE for h in hyps}
    per[hyps[0]] = NeedObtainability.OBTAINABLE  # one datum still gettable
    _declare(case, per)
    assert assess_verification_status(case) == VerificationStatus.OPEN


def test_unknown_obtainability_is_fail_safe():
    # The default (no declaration) never establishes a wall.
    case = _work_done()
    _declare(case, {h: NeedObtainability.UNKNOWN for h in case.hypotheses.keys()})
    assert assess_verification_status(case) == VerificationStatus.OPEN


def test_declared_wall_never_bypasses_work_gate():
    # Below the work gate, even a fully-declared wall cannot reach insufficient —
    # it is a provider-health fact, not a case verdict.
    case = _case(
        grounded=False,
        n_hypotheses=1,
        n_categories=1,
        n_evidence=1,
        current_turn=3,
        turns_without_progress=0,
    )
    hyp_id = next(iter(case.hypotheses.keys()))
    _declare(case, {hyp_id: NeedObtainability.UNOBTAINABLE})
    assert not work_gate_passed(case)
    assert assess_verification_status(case) == VerificationStatus.NOT_YET_PRODUCTIVE


def test_refuted_candidates_excluded_from_the_wall():
    # A REFUTED candidate is not residual; only live candidates' walls count. With
    # the refuted one excluded, the remaining residual candidates are all walled.
    case = _work_done()
    hyps = list(case.hypotheses.values())
    object.__setattr__(hyps[0], "state", HypothesisState.REFUTED)
    _declare(
        case,
        {h.hypothesis_id: NeedObtainability.UNOBTAINABLE for h in hyps[1:]},
    )
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE


def test_wall_does_not_flip_a_grounded_case():
    # The declared wall is about GROUNDING, not fix-reachability. On a grounded
    # case it must NOT flip HEALTHY → TREATMENT_BLOCKED — the wall arm is scoped
    # to the not-grounded branch.
    grounded = _case(
        grounded=True,
        n_hypotheses=3,
        n_categories=2,
        n_evidence=2,
        current_turn=3,  # not time-stalled
        turns_without_progress=0,
    )
    _declare(
        grounded,
        {h: NeedObtainability.UNOBTAINABLE for h in grounded.hypotheses.keys()},
    )
    assert assess_verification_status(grounded) == VerificationStatus.HEALTHY
    # Grounded + TIME-stalled is still TREATMENT_BLOCKED (fix-reachability stall).
    grounded_stalled = _case(
        grounded=True,
        n_hypotheses=3,
        n_categories=2,
        n_evidence=2,
        current_turn=9,
        turns_without_progress=5,
    )
    assert (
        assess_verification_status(grounded_stalled)
        == VerificationStatus.TREATMENT_BLOCKED
    )


# --- Known false positive: slow-but-live via the TIME arm --------------------
# The time arm (current_turn ≥ N ∧ turns_without_progress ≥ N) cannot tell "the
# investigation is walled" from "the user was just slow to respond." A case whose
# discriminating data is explicitly OBTAINABLE — so it is NOT a declared wall —
# still trips INSUFFICIENT_EVIDENCE on the time thresholds alone. This is an
# ACCEPTED false positive (design §5.5 / no-collapse rule 4), not a bug:
#   - it is indistinguishable from a real wall in pure case state, so no work-gate
#     tightening removes it without breaking the declared-wall arm (whose residual
#     candidates are un-refuted by definition — tightening to ≥2-refuted would
#     make the handoff never fire on the canonical unobservable-cause archetype);
#   - it is non-terminal and self-dissolves the moment progress resumes
#     (fail-forward), so it never traps a live case;
#   - its *rate* is the property the live/simulator calibration tier measures on
#     real trajectories (the handoff ships as a soundness fix, validated by
#     simulation rather than a flag). This deterministic tier can only pin that
#     the arm fires and that it self-heals; it cannot measure how often a real
#     trajectory is genuinely still-live.


def test_slow_but_live_is_a_known_time_arm_false_positive():
    case = _work_done(current_turn=9, turns_without_progress=5)
    # Data IS obtainable — this is NOT a wall; the user was merely slow.
    _declare(case, {h: NeedObtainability.OBTAINABLE for h in case.hypotheses.keys()})
    # The time arm fires regardless → the accepted false positive.
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE
    # Prove the verdict rode the TIME arm, not a (non-existent) declared wall:
    # clear only the time thresholds and it drops straight back to OPEN.
    case.turns_without_progress = 2
    assert assess_verification_status(case) == VerificationStatus.OPEN


def test_time_arm_false_positive_fails_forward_when_the_cause_is_grounded():
    # The other no-collapse path: the mislabel never blocks a later success. Test
    # 1 shows it dissolving to OPEN when the user simply resumes; here the cause is
    # actually grounded on a later turn (and progress resumes), and the status
    # fail-forwards straight out of INSUFFICIENT_EVIDENCE to HEALTHY. A slow-but-
    # live FP is never a dead end for a case that then gets solved.
    case = _work_done(current_turn=20, turns_without_progress=8)
    _declare(case, {h: NeedObtainability.OBTAINABLE for h in case.hypotheses.keys()})
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE
    # A validated ROOT lands (grade → GROUNDED) and progress resumes.
    d, root = _problem(), _grounded_root()
    case.causal_nodes = {d.node_id: d, root.node_id: root}
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=d.node_id)
    ]
    case.turns_without_progress = 0
    assert assess_verification_status(case) == VerificationStatus.HEALTHY
