"""Gate 1b: ``derive_node_states`` — evidence-grounded node validation (§7.1).

A causal node reaches VALIDATED only on real CAUSAL_EVIDENCE-backed support plus
the M7 AND-gate — never from a fabricated EMPIRICAL grade.
``cause_state=IDENTIFIED`` then reads
``is_chain_root_validated`` (§9.2), so these tests pin what makes a root real.
"""

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import (
    derive_node_states,
    is_chain_root_validated,
)
from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    cause_is_runbook_grounded,
    cause_validation_is_fallback_only,
    count_grounded_roots,
    grade_cause_assurance,
    support_is_runbook_grounded,
)
from faultmaven.core.investigation.grounding_metrics import (
    compute_grounding_baseline,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _nid(seed: int) -> str:
    return f"cn_{seed:012x}"


def _node(node_id, *, node_type=NodeType.INTERMEDIATE, links=None) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=f"node {node_id}",
        node_type=node_type,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=node_type == NodeType.ROOT,
        evidence_links=links or [],
        generated_at_turn=1,
    )


def _eid(label: str) -> str:
    """Deterministic valid evidence id (^ev_[a-f0-9]{12}$) from a readable label."""
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _link(label, stance) -> NodeEvidenceLink:
    return NodeEvidenceLink(
        evidence_id=_eid(label),
        stance=stance,
        reasoning="bears on the rung",
        linked_at_turn=2,
    )


def _evidence(label, category) -> Evidence:
    return Evidence(
        evidence_id=_eid(label),
        summary="an observed fact",
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _case(nodes, edges=None, evidence=None, hyps=None) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="X fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="X fails", severity=CaseSeverity.HIGH
        ),
    )
    case.causal_nodes = {n.node_id: n for n in nodes}
    case.causal_edges = edges or []
    case.evidence = evidence or []
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    return case


# ---------------------------------------------------------------------------
# Empirical validation (§7.1)
# ---------------------------------------------------------------------------


def test_causal_supports_validates_a_root():
    ev = _evidence("ev_causal", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(1),
        node_type=NodeType.ROOT,
        links=[_link("ev_causal", EvidenceStance.SUPPORTS)],
    )
    case = _case([root], evidence=[ev])
    changed = derive_node_states(case)
    assert changed is True
    assert root.node_state == NodeState.VALIDATED
    assert root.validation_method == ValidationMethod.EMPIRICAL
    assert root.actionable is True  # M1


def test_symptom_backed_support_does_not_validate():
    """Only CAUSAL_EVIDENCE clears the §7.1 bar; a symptom-backed SUPPORTS does
    not validate (it leaves the node INCONCLUSIVE, not VALIDATED)."""
    ev = _evidence("ev_symptom", EvidenceCategory.SYMPTOM_EVIDENCE)
    n = _node(_nid(2), links=[_link("ev_symptom", EvidenceStance.SUPPORTS)])
    case = _case([n], evidence=[ev])
    derive_node_states(case)
    assert n.node_state == NodeState.INCONCLUSIVE


def test_net_refuting_evidence_refutes_node():
    ev = _evidence("ev_ref", EvidenceCategory.CAUSAL_EVIDENCE)
    n = _node(_nid(3), links=[_link("ev_ref", EvidenceStance.REFUTES)])
    case = _case([n], evidence=[ev])
    derive_node_states(case)
    assert n.node_state == NodeState.REFUTED
    assert n.refutation_reason  # required, set so the node reloads
    assert n.validation_method == ValidationMethod.NONE


def test_tie_supports_equal_refutes_is_inconclusive():
    """A support/refute tie is genuinely INCONCLUSIVE — neither side wins, so it
    is not asserted REFUTED (refutation needs strictly more refutes)."""
    evs = _evidence("ev_s", EvidenceCategory.CAUSAL_EVIDENCE)
    evr = _evidence("ev_r", EvidenceCategory.CAUSAL_EVIDENCE)
    n = _node(
        _nid(4),
        links=[
            _link("ev_s", EvidenceStance.SUPPORTS),
            _link("ev_r", EvidenceStance.REFUTES),
        ],
    )
    case = _case([n], evidence=[evs, evr])
    derive_node_states(case)
    assert n.node_state == NodeState.INCONCLUSIVE


def test_counterfactual_absence_refutes_decisively_over_support():
    """A CAUSAL_ABSENCE_EVIDENCE REFUTES (counterfactual disconfirmation, §7.2) is
    decisive — it refutes even against an equal causal SUPPORTS (where an ordinary
    correlational tie would be INCONCLUSIVE)."""
    sup = _evidence("ev_sup", EvidenceCategory.CAUSAL_EVIDENCE)
    absent = _evidence("ev_absent", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    n = _node(
        _nid(8),
        links=[
            _link("ev_sup", EvidenceStance.SUPPORTS),
            _link("ev_absent", EvidenceStance.REFUTES),
        ],
    )
    case = _case([n], evidence=[sup, absent])
    derive_node_states(case)
    assert n.node_state == NodeState.REFUTED


def test_no_evidence_stays_candidate():
    n = _node(_nid(5))
    case = _case([n])
    changed = derive_node_states(case)
    assert changed is False
    assert n.node_state == NodeState.CANDIDATE


def test_dangling_evidence_ref_is_ignored():
    n = _node(_nid(6), links=[_link("ev_missing", EvidenceStance.SUPPORTS)])
    case = _case([n], evidence=[])  # no backing row
    derive_node_states(case)
    assert n.node_state == NodeState.CANDIDATE


def test_problem_node_is_left_untouched():
    d = _node(_nid(7), node_type=NodeType.PROBLEM)
    ev = _evidence("ev_c", EvidenceCategory.CAUSAL_EVIDENCE)
    d.evidence_links = [_link("ev_c", EvidenceStance.SUPPORTS)]
    case = _case([d], evidence=[ev])
    derive_node_states(case)
    assert d.node_state == NodeState.CANDIDATE  # engine-owned anchor, never derived


# ---------------------------------------------------------------------------
# M7 AND-gate + fixpoint
# ---------------------------------------------------------------------------


def test_and_gate_blocks_then_unlocks_effect_in_one_pass():
    """An effect with two co-necessary causes validates only once BOTH causes do
    — and the fixpoint settles it within a single derive call."""
    ev = _evidence("ev_c", EvidenceCategory.CAUSAL_EVIDENCE)
    c1 = _node(_nid(10), links=[_link("ev_c", EvidenceStance.SUPPORTS)])
    c2 = _node(_nid(11), links=[_link("ev_c", EvidenceStance.SUPPORTS)])
    effect = _node(_nid(12), links=[_link("ev_c", EvidenceStance.SUPPORTS)])
    edges = [
        CausalEdge(
            cause_node_id=c1.node_id, effect_node_id=effect.node_id, and_group="g"
        ),
        CausalEdge(
            cause_node_id=c2.node_id, effect_node_id=effect.node_id, and_group="g"
        ),
    ]
    case = _case([c1, c2, effect], edges=edges, evidence=[ev])
    derive_node_states(case)
    assert c1.node_state == NodeState.VALIDATED
    assert c2.node_state == NodeState.VALIDATED
    assert effect.node_state == NodeState.VALIDATED


def test_and_gate_refuted_member_blocks_effect_validation():
    evc = _evidence("ev_c", EvidenceCategory.CAUSAL_EVIDENCE)
    evr = _evidence("ev_r", EvidenceCategory.CAUSAL_EVIDENCE)
    c1 = _node(_nid(20), links=[_link("ev_c", EvidenceStance.SUPPORTS)])
    c2 = _node(
        _nid(21), links=[_link("ev_r", EvidenceStance.REFUTES)]
    )  # refuted member
    effect = _node(_nid(22), links=[_link("ev_c", EvidenceStance.SUPPORTS)])
    edges = [
        CausalEdge(
            cause_node_id=c1.node_id, effect_node_id=effect.node_id, and_group="g"
        ),
        CausalEdge(
            cause_node_id=c2.node_id, effect_node_id=effect.node_id, and_group="g"
        ),
    ]
    case = _case([c1, c2, effect], edges=edges, evidence=[evc, evr])
    derive_node_states(case)
    assert c2.node_state == NodeState.REFUTED
    # The AND-gate BLOCKS validation (M7 proof needs every member validated), but
    # derive does not structurally REFUTE the effect — it has its own support, so
    # it stays INCONCLUSIVE (conservative; structural refutation is §9.4).
    assert effect.node_state == NodeState.INCONCLUSIVE


def test_refuted_and_member_does_not_over_refute_effect_with_or_alternative():
    """A refuted member of one AND-group must NOT refute an effect that also has
    an independent OR-alternative path — the over-refutation bug. derive does no
    structural refutation, so the effect is never REFUTED on a sibling's account."""
    evr = _evidence("ev_r", EvidenceCategory.CAUSAL_EVIDENCE)
    evc = _evidence("ev_c", EvidenceCategory.CAUSAL_EVIDENCE)
    a = _node(
        _nid(50), links=[_link("ev_r", EvidenceStance.REFUTES)]
    )  # refuted AND member
    b = _node(_nid(51), links=[_link("ev_c", EvidenceStance.SUPPORTS)])  # valid OR alt
    effect = _node(_nid(52))  # no own evidence yet
    edges = [
        CausalEdge(
            cause_node_id=a.node_id, effect_node_id=effect.node_id, and_group="g1"
        ),
        CausalEdge(
            cause_node_id=b.node_id, effect_node_id=effect.node_id, and_group=None
        ),
    ]
    case = _case([a, b, effect], edges=edges, evidence=[evr, evc])
    derive_node_states(case)
    assert a.node_state == NodeState.REFUTED
    assert effect.node_state != NodeState.REFUTED  # the fix: no over-refutation


# ---------------------------------------------------------------------------
# Round-trip safety (the model validators run on reload)
# ---------------------------------------------------------------------------


def test_derived_states_round_trip_through_model_validators():
    """Whatever derive sets must reload via CausalNode(**dump) without tripping
    the M1/M4/refutation validators."""
    evc = _evidence("ev_c", EvidenceCategory.CAUSAL_EVIDENCE)
    evr = _evidence("ev_r", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(30),
        node_type=NodeType.ROOT,
        links=[_link("ev_c", EvidenceStance.SUPPORTS)],
    )
    refuted = _node(_nid(31), links=[_link("ev_r", EvidenceStance.REFUTES)])
    case = _case([root, refuted], evidence=[evc, evr])
    derive_node_states(case)
    for n in (root, refuted):
        CausalNode(**n.model_dump())  # raises if an invariant combination is wrong


# ---------------------------------------------------------------------------
# Deductive preservation + idempotency
# ---------------------------------------------------------------------------


def test_deductively_validated_node_is_not_demoted():
    """A node VALIDATED by deduction (§7.1.1) carries no supporting evidence of
    its own; the empirical lane must leave it intact, not demote it to CANDIDATE."""
    n = _node(_nid(60), node_type=NodeType.ROOT)
    n.node_state = NodeState.VALIDATED
    n.validation_method = ValidationMethod.DEDUCTIVE
    n.actionable = True
    case = _case([n])  # no evidence
    derive_node_states(case)
    assert n.node_state == NodeState.VALIDATED
    assert n.validation_method == ValidationMethod.DEDUCTIVE


def test_deductive_node_is_overturned_by_direct_refutation():
    evr = _evidence("ev_r", EvidenceCategory.CAUSAL_EVIDENCE)
    n = _node(_nid(61), links=[_link("ev_r", EvidenceStance.REFUTES)])
    n.node_state = NodeState.VALIDATED
    n.validation_method = ValidationMethod.DEDUCTIVE
    case = _case([n], evidence=[evr])
    derive_node_states(case)
    assert n.node_state == NodeState.REFUTED


def test_redrive_is_idempotent():
    """A second derive over a settled graph changes nothing (no needless persist)."""
    ev = _evidence("ev_c", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(70),
        node_type=NodeType.ROOT,
        links=[_link("ev_c", EvidenceStance.SUPPORTS)],
    )
    case = _case([root], evidence=[ev])
    assert derive_node_states(case) is True
    assert derive_node_states(case) is False  # already settled


# ---------------------------------------------------------------------------
# Integration with is_chain_root_validated (what cause_state reads)
# ---------------------------------------------------------------------------


def test_validated_root_makes_chain_root_validated():
    ev = _evidence("ev_c", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(40),
        node_type=NodeType.ROOT,
        links=[_link("ev_c", EvidenceStance.SUPPORTS)],
    )
    hyp = Hypothesis(
        hypothesis_id="hyp_000000000001",
        statement="the cause",
        category=HypothesisCategory.CONFIG,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="the deepest posited cause",
        root_node_id=root.node_id,
        generated_at_turn=1,
    )
    case = _case([root], evidence=[ev], hyps=[hyp])
    assert is_chain_root_validated(hyp, case.causal_nodes) is False
    derive_node_states(case)
    assert is_chain_root_validated(hyp, case.causal_nodes) is True


# ---------------------------------------------------------------------------
# Validation-assurance from provenance (cause_validation_is_fallback_only)
# ---------------------------------------------------------------------------


def _plink(label, stance, provenance) -> NodeEvidenceLink:
    return NodeEvidenceLink(
        evidence_id=_eid(label),
        stance=stance,
        reasoning="bears on the rung",
        provenance=provenance,
        linked_at_turn=2,
    )


def _validated_root_case(provenances) -> Case:
    """A case with one ROOT validated empirically by CAUSAL_EVIDENCE support
    links carrying the given provenances."""
    evs = [
        _evidence(f"ev_{i}", EvidenceCategory.CAUSAL_EVIDENCE)
        for i in range(len(provenances))
    ]
    links = [
        _plink(f"ev_{i}", EvidenceStance.SUPPORTS, p) for i, p in enumerate(provenances)
    ]
    root = _node(_nid(50), node_type=NodeType.ROOT, links=links)
    case = _case([root], evidence=evs)
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED  # precondition for the signal
    return case


def test_fallback_only_validation_is_flagged():
    # A root borne out solely by an llm_fallback-provenance causal support is a
    # lower-assurance identification.
    case = _validated_root_case(["llm_fallback"])
    assert cause_validation_is_fallback_only(case) is True


def test_runbook_provenance_is_sound():
    case = _validated_root_case(["runbook"])
    assert cause_validation_is_fallback_only(case) is False


def test_unlabeled_none_provenance_is_lower_assurance():
    # provenance=None is the LLM's own asserted link (the emitted-chain path never
    # tags provenance) — NOT authority-grounded, so a root borne out only by it is
    # fallback-only. Only a runbook-grounded support is sound.
    case = _validated_root_case([None])
    assert cause_validation_is_fallback_only(case) is True


def test_any_sound_support_makes_validation_sound():
    # A single runbook-grounded support alongside a fallback one is enough.
    case = _validated_root_case(["llm_fallback", "runbook"])
    assert cause_validation_is_fallback_only(case) is False


def test_no_validated_root_is_not_fallback_only():
    # The signal applies only once a cause is identified.
    ev = _evidence("ev_x", EvidenceCategory.SYMPTOM_EVIDENCE)
    root = _node(
        _nid(51),
        node_type=NodeType.ROOT,
        links=[_plink("ev_x", EvidenceStance.SUPPORTS, "llm_fallback")],
    )
    case = _case([root], evidence=[ev])
    derive_node_states(case)
    assert root.node_state != NodeState.VALIDATED  # symptom-backed, not validated
    assert cause_validation_is_fallback_only(case) is False


def test_deductively_validated_root_is_sound():
    # Proof-by-exclusion is a methodology derivation, not an LLM-fallback grade.
    root = _node(_nid(52), node_type=NodeType.ROOT)
    root.node_state = NodeState.VALIDATED
    root.validation_method = ValidationMethod.DEDUCTIVE
    case = _case([root])
    assert cause_validation_is_fallback_only(case) is False


# ---------------------------------------------------------------------------
# Harvest bar from provenance (cause_is_runbook_grounded) — #590 A1/A2
# ---------------------------------------------------------------------------


def test_runbook_grounded_true_for_runbook_support():
    case = _validated_root_case(["runbook"])
    assert cause_is_runbook_grounded(case) is True


def test_runbook_grounded_false_for_fallback_only():
    case = _validated_root_case(["llm_fallback"])
    assert cause_is_runbook_grounded(case) is False


def test_runbook_grounded_true_when_any_support_is_runbook():
    case = _validated_root_case(["llm_fallback", "runbook"])
    assert cause_is_runbook_grounded(case) is True


def test_runbook_grounded_true_for_deductive_root():
    root = _node(_nid(53), node_type=NodeType.ROOT)
    root.node_state = NodeState.VALIDATED
    root.validation_method = ValidationMethod.DEDUCTIVE
    case = _case([root])
    assert cause_is_runbook_grounded(case) is True


def test_runbook_grounded_false_when_no_validated_root():
    # #590 A1: a RootCauseConclusion may carry ZERO causal graph (pure LLM prose).
    # With no validated root there is nothing authority-grounded, so harvest must be
    # held. The negative "fallback-only" view returns False here too — which is
    # exactly why gating harvest on it leaked an ungrounded cause. The positive bar
    # returns False (not grounded), closing the hole.
    case = _case([])  # no nodes at all — bare RCC prose shape
    assert cause_is_runbook_grounded(case) is False
    assert cause_validation_is_fallback_only(case) is False  # the leak it replaces


def test_runbook_support_validates_regardless_of_evidence_category():
    # #590 A2: a runbook-provenance SUPPORTS is causal grounding even when the LLM
    # filed the backing datum as SYMPTOM_EVIDENCE — the deterministic predicate
    # fired against the telemetry, which is causal independent of the orthogonal
    # category label. (Contrast test_symptom_backed_support_does_not_validate: a
    # None-provenance, LLM-asserted symptom support does NOT validate.)
    ev = _evidence("ev_rb_sym", EvidenceCategory.SYMPTOM_EVIDENCE)
    root = _node(
        _nid(54),
        node_type=NodeType.ROOT,
        links=[_plink("ev_rb_sym", EvidenceStance.SUPPORTS, "runbook")],
    )
    case = _case([root], evidence=[ev])
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED
    assert cause_is_runbook_grounded(case) is True


# ---------------------------------------------------------------------------
# The single assurance grade (grade_cause_assurance) + its shared primitive
# ---------------------------------------------------------------------------


def test_support_is_runbook_grounded_primitive():
    # The one home for "runbook provenance is causal grounding". Only a
    # runbook-provenance SUPPORTS qualifies — stance and provenance both matter.
    assert (
        support_is_runbook_grounded(_plink("ev_a", EvidenceStance.SUPPORTS, "runbook"))
        is True
    )
    assert (
        support_is_runbook_grounded(
            _plink("ev_a", EvidenceStance.SUPPORTS, "llm_fallback")
        )
        is False
    )
    assert (
        support_is_runbook_grounded(_plink("ev_a", EvidenceStance.SUPPORTS, None))
        is False
    )
    # A runbook-provenance REFUTES is not a grounding SUPPORTS.
    assert (
        support_is_runbook_grounded(_plink("ev_a", EvidenceStance.REFUTES, "runbook"))
        is False
    )


def test_grade_is_grounded_for_runbook_support():
    case = _validated_root_case(["runbook"])
    assert grade_cause_assurance(case) == CauseAssuranceGrade.GROUNDED


def test_grade_is_grounded_for_deductive_root():
    root = _node(_nid(55), node_type=NodeType.ROOT)
    root.node_state = NodeState.VALIDATED
    root.validation_method = ValidationMethod.DEDUCTIVE
    case = _case([root])
    assert grade_cause_assurance(case) == CauseAssuranceGrade.GROUNDED


def test_grade_is_fallback_only_for_lower_assurance_support():
    case = _validated_root_case(["llm_fallback"])
    assert grade_cause_assurance(case) == CauseAssuranceGrade.FALLBACK_ONLY


def test_grade_is_no_root_for_bare_rcc():
    # No validated root at all (#590 A1) — distinct from FALLBACK_ONLY. This is the
    # state the negative "fallback-only" view conflated into False, leaking harvest.
    case = _case([])
    assert grade_cause_assurance(case) == CauseAssuranceGrade.NO_ROOT


def test_boolean_views_agree_with_grade_across_all_states():
    # The wrappers can never disagree with the grade — that consistency is the
    # whole point of routing both through grade_cause_assurance.
    cases = {
        CauseAssuranceGrade.GROUNDED: _validated_root_case(["runbook"]),
        CauseAssuranceGrade.FALLBACK_ONLY: _validated_root_case(["llm_fallback"]),
        CauseAssuranceGrade.NO_ROOT: _case([]),
    }
    for grade, case in cases.items():
        assert grade_cause_assurance(case) == grade
        assert cause_is_runbook_grounded(case) is (
            grade == CauseAssuranceGrade.GROUNDED
        )
        assert cause_validation_is_fallback_only(case) is (
            grade == CauseAssuranceGrade.FALLBACK_ONLY
        )


# ---------------------------------------------------------------------------
# Phase 0b — per-case grounded-root counter (count_grounded_roots).
# Drift-lock: it mirrors grade_cause_assurance, so R1 equivalence
# (grade == GROUNDED  iff  grounded_roots >= 1) must hold on every fixture.
# ---------------------------------------------------------------------------


def _both_arms_root_case() -> Case:
    """One VALIDATED root grounded by BOTH arms — a runbook SUPPORTS link AND a
    DEDUCTIVE validation_method. The overlap fixture for R2 sum-safety."""
    ev = _evidence("ev_both", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(60),
        node_type=NodeType.ROOT,
        links=[_plink("ev_both", EvidenceStance.SUPPORTS, "runbook")],
    )
    root.node_state = NodeState.VALIDATED
    root.validation_method = ValidationMethod.DEDUCTIVE
    return _case([root], evidence=[ev])


def test_count_r1_equivalence_across_all_grades():
    # The ratification test for R1: grade == GROUNDED  iff  grounded_roots >= 1, on
    # the SAME fixtures the grade tests use. If these ever disagree the counter has
    # drifted from the gate.
    fixtures = {
        CauseAssuranceGrade.GROUNDED: _validated_root_case(["runbook"]),
        CauseAssuranceGrade.FALLBACK_ONLY: _validated_root_case(["llm_fallback"]),
        CauseAssuranceGrade.NO_ROOT: _case([]),
    }
    for grade, case in fixtures.items():
        tally = count_grounded_roots(case)
        assert (tally.grounded_roots >= 1) is (
            grade_cause_assurance(case) == CauseAssuranceGrade.GROUNDED
        )
        assert grade_cause_assurance(case) == grade  # fixture sanity


def test_count_runbook_arm_only():
    tally = count_grounded_roots(_validated_root_case(["runbook"]))
    assert tally.grounded_roots == 1
    assert tally.runbook_arm == 1
    assert tally.deductive_arm == 0
    assert tally.validated_roots == 1


def test_count_deductive_arm_only():
    root = _node(_nid(56), node_type=NodeType.ROOT)
    root.node_state = NodeState.VALIDATED
    root.validation_method = ValidationMethod.DEDUCTIVE
    tally = count_grounded_roots(_case([root]))
    assert tally.grounded_roots == 1
    assert tally.deductive_arm == 1
    assert tally.runbook_arm == 0


def test_count_r2_arms_are_non_exclusive_and_must_not_sum():
    # A root grounded by both arms is ONE grounded root but appears in BOTH arm
    # tallies — so runbook_arm + deductive_arm (2) > grounded_roots (1). Reporting the
    # sum as the total would double-count; this pins the sum-safety invariant.
    tally = count_grounded_roots(_both_arms_root_case())
    assert tally.grounded_roots == 1
    assert tally.runbook_arm == 1
    assert tally.deductive_arm == 1
    assert tally.runbook_arm + tally.deductive_arm > tally.grounded_roots


def test_count_ignores_dangling_runbook_link():
    # A runbook SUPPORTS link whose evidence was deleted (not in case.evidence) never
    # grounds — mirrors grade_cause_assurance's dangling guard.
    root = _node(
        _nid(57),
        node_type=NodeType.ROOT,
        links=[_plink("ev_gone", EvidenceStance.SUPPORTS, "runbook")],
    )
    root.node_state = NodeState.VALIDATED
    root.validation_method = ValidationMethod.EMPIRICAL
    case = _case([root], evidence=[])  # dangling: evidence absent
    tally = count_grounded_roots(case)
    assert tally.grounded_roots == 0
    assert tally.runbook_arm == 0
    # Equivalence still holds: grade is not GROUNDED either.
    assert grade_cause_assurance(case) != CauseAssuranceGrade.GROUNDED


def test_count_links_fired_counts_intermediate_nodes_but_not_as_grounding():
    # Leading indicator: a runbook predicate that fired on an INTERMEDIATE rung (no
    # validated root yet) — the earliest proof the loop is alive, but NOT a grounded
    # root. runbook_links_fired moves off 0 while grounded_roots stays 0.
    ev = _evidence("ev_mid", EvidenceCategory.CAUSAL_EVIDENCE)
    inter = _node(
        _nid(58),
        node_type=NodeType.INTERMEDIATE,
        links=[_plink("ev_mid", EvidenceStance.SUPPORTS, "runbook")],
    )
    case = _case([inter], evidence=[ev])
    tally = count_grounded_roots(case)
    assert tally.grounded_roots == 0
    assert tally.validated_roots == 0
    assert tally.runbook_links_fired == 1


# ---------------------------------------------------------------------------
# Phase 0b — grounding baseline aggregation (compute_grounding_baseline).
# R3: the denominator is the retrieval marker (runbook_retrieved), NEVER the
# verdict-gated differential_runbook_ids — so a non-zero denominator can sit
# over a zero numerator (the dead-arm baseline this metric exists to expose).
# ---------------------------------------------------------------------------


def test_baseline_r3_denominator_excludes_non_retrieved_cases():
    # A grounded case that never had a runbook retrieved is NOT in the matching-runbook
    # population — it must not inflate the denominator.
    grounded = _validated_root_case(["runbook"])
    grounded.runbook_retrieved = False
    baseline = compute_grounding_baseline([grounded])
    assert baseline.population == 0
    assert baseline.grounded_rate == 0.0


def test_baseline_r3_non_circular_zero_numerator_over_nonzero_denominator():
    # The finding this metric was built to surface: cases WHERE a runbook was retrieved
    # (real denominator) but nothing grounded (dead arms) — a 7%-style dead baseline,
    # distinct from a meaningless 0/0.
    retrieved_but_ungrounded = _validated_root_case(["llm_fallback"])
    retrieved_but_ungrounded.runbook_retrieved = True
    baseline = compute_grounding_baseline([retrieved_but_ungrounded])
    assert baseline.population == 1  # denominator is real
    assert baseline.grounded_roots == 0  # numerator is dead
    assert baseline.runbook_arm_roots == 0
    assert baseline.deductive_arm_roots == 0


def test_baseline_counts_grounded_case_in_population():
    grounded = _validated_root_case(["runbook"])
    grounded.runbook_retrieved = True
    baseline = compute_grounding_baseline([grounded])
    assert baseline.population == 1
    assert baseline.cases_grounded == 1
    assert baseline.grounded_roots == 1
    assert baseline.runbook_arm_roots == 1
    assert baseline.grounded_rate == 1.0


def test_baseline_terminal_scope_filters_non_terminal_cases():
    investigating = _validated_root_case(["runbook"])
    investigating.runbook_retrieved = True
    investigating.state = CaseState.INVESTIGATING
    resolved = _validated_root_case(["runbook"])
    resolved.runbook_retrieved = True
    # Bypass the RESOLVED cross-field validator (needs resolved_at/closed_at/closure
    # metadata) — the aggregation only reads case.state, so the disposition timestamps
    # are irrelevant to this test.
    object.__setattr__(resolved, "state", CaseState.RESOLVED)

    all_scope = compute_grounding_baseline([investigating, resolved], scope="all")
    terminal_scope = compute_grounding_baseline(
        [investigating, resolved], scope="terminal"
    )
    assert all_scope.population == 2  # both reached INVESTIGATING
    assert terminal_scope.population == 1  # only the RESOLVED case is harvest-ready


def test_baseline_empty_population_rate_is_zero():
    baseline = compute_grounding_baseline([])
    assert baseline.population == 0
    assert baseline.grounded_rate == 0.0
