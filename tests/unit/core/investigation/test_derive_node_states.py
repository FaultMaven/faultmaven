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
    grade_cause_assurance,
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


def _link(label, stance, stance_confidence=1.0) -> NodeEvidenceLink:
    return NodeEvidenceLink(
        evidence_id=_eid(label),
        stance=stance,
        reasoning="bears on the rung",
        linked_at_turn=2,
        stance_confidence=stance_confidence,
    )


def _evidence(label, category) -> Evidence:
    # Label-derived summary: two fixture rows read as INDEPENDENT observations
    # under the INV-29 mirror collapse (identical summaries would be one).
    return Evidence(
        evidence_id=_eid(label),
        summary=f"fact-{label} metric-{label} reading-{label}",
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
    # Two INDEPENDENT causal supports — the INV-29 ROOT bar.
    evs = [
        _evidence("ev_causal", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("ev_causal_b", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    root = _node(
        _nid(1),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_causal", EvidenceStance.SUPPORTS),
            _link("ev_causal_b", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case([root], evidence=evs)
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
    evs = [
        _evidence("ev_c", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("ev_c2", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    root = _node(
        _nid(40),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_c", EvidenceStance.SUPPORTS),
            _link("ev_c2", EvidenceStance.SUPPORTS),
        ],
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
    case = _case([root], evidence=evs, hyps=[hyp])
    assert is_chain_root_validated(hyp, case.causal_nodes) is False
    derive_node_states(case)
    assert is_chain_root_validated(hyp, case.causal_nodes) is True


# ---------------------------------------------------------------------------
# Cause-assurance grade (grade_cause_assurance) — §7 harvest bar
# ---------------------------------------------------------------------------


def _validated_root(*, method: ValidationMethod) -> CausalNode:
    """A VALIDATED root with the given validation method (constructed valid so
    the M1/M4 model-validators hold: a validated root is actionable and carries
    a method)."""
    return CausalNode(
        node_id=_nid(0xA0),
        statement="the posited root cause",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=method,
        belief=0.9,
        actionable=True,
        evidence_links=[_link("ev_root", EvidenceStance.SUPPORTS)],
        generated_at_turn=1,
    )


def test_grade_is_mechanistic_for_deductive_root():
    # Proof-by-exclusion (§7.1.1) validates, but validation is mechanistic
    # grade (M2): a deductive derivation rests on model-mediated refutations
    # plus an asserted-exhaustive differential, so it does NOT clear the §7
    # harvest bar (CONFIRMED) on its own.
    case = _case(
        [_validated_root(method=ValidationMethod.DEDUCTIVE)],
        evidence=[_evidence("ev_root", EvidenceCategory.CAUSAL_EVIDENCE)],
    )
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


def test_grade_is_mechanistic_for_empirical_root():
    # An EMPIRICAL (LLM-mediated) validation is graph-identified but NOT
    # confirmed — it must not auto-seed reusable knowledge.
    case = _case(
        [_validated_root(method=ValidationMethod.EMPIRICAL)],
        evidence=[_evidence("ev_root", EvidenceCategory.CAUSAL_EVIDENCE)],
    )
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


def test_grade_is_confirmed_for_counterfactually_confirmed_root():
    # M2 gone⇒gone: a validated root bearing a SUPPORTS link backed by a
    # causal_absence_evidence row (cause removed, problem gone) is CONFIRMED —
    # the sole harvest authority and the only grade that reads "verified".
    root = _validated_root(method=ValidationMethod.EMPIRICAL)
    root.evidence_links.append(_link("ev_absence", EvidenceStance.SUPPORTS))
    case = _case(
        [root],
        evidence=[
            _evidence("ev_root", EvidenceCategory.CAUSAL_EVIDENCE),
            _evidence("ev_absence", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE),
        ],
    )
    assert grade_cause_assurance(case) == CauseAssuranceGrade.CONFIRMED


def test_grade_ignores_unlinked_causal_absence_row():
    # Bearing discipline: a case-level causal_absence row with NO link to the
    # validated root does not confirm it — the confirmation must bear on the
    # root it confirms.
    case = _case(
        [_validated_root(method=ValidationMethod.EMPIRICAL)],
        evidence=[
            _evidence("ev_root", EvidenceCategory.CAUSAL_EVIDENCE),
            _evidence("ev_absence", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE),
        ],
    )
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


def test_grade_is_no_root_for_bare_rcc():
    # No VALIDATED root at all (a pure LLM-authored conclusion, #590 A1) is
    # NO_ROOT — distinct from fallback-only, and equally held back from harvest.
    root = _node(_nid(0xA1), node_type=NodeType.ROOT)
    case = _case([root])
    assert grade_cause_assurance(case) == CauseAssuranceGrade.NO_ROOT


def test_grade_ignores_non_root_deductive_nodes():
    # Only a validated ROOT can ground the case grade; a deductively validated
    # intermediate rung does not clear the bar.
    rung = CausalNode(
        node_id=_nid(0xA2),
        statement="an intermediate rung",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.DEDUCTIVE,
        belief=0.9,
        actionable=False,
        evidence_links=[_link("ev_rung", EvidenceStance.SUPPORTS)],
        generated_at_turn=1,
    )
    case = _case(
        [rung], evidence=[_evidence("ev_rung", EvidenceCategory.CAUSAL_EVIDENCE)]
    )
    assert grade_cause_assurance(case) == CauseAssuranceGrade.NO_ROOT


# ---------------------------------------------------------------------------
# §7.1 restatement guard — the symptom dressed as a cause never validates
# ---------------------------------------------------------------------------

_D_STATEMENT = "Intermittent 502 errors under load"


def _problem_node() -> CausalNode:
    return _node(_nid(0xD0), node_type=NodeType.PROBLEM)


def _hyp(statement: str, *, root_node_id=None) -> Hypothesis:
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.OTHER,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="posited",
        root_node_id=root_node_id,
        generated_at_turn=1,
    )


def _anchored_case(root_statement: str, *, node_type=NodeType.ROOT, hyps=None):
    """A case with a PROBLEM anchor D and a node under test carrying TWO
    independent causal supports (the INV-29 bar) — these tests exercise the
    restatement guard, not the support count."""
    d = _problem_node()
    object.__setattr__(d, "statement", _D_STATEMENT)
    evs = [
        _evidence("ev_causal", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("ev_causal_b", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    n = _node(
        _nid(0xD1),
        node_type=node_type,
        links=[
            _link("ev_causal", EvidenceStance.SUPPORTS),
            _link("ev_causal_b", EvidenceStance.SUPPORTS),
        ],
    )
    object.__setattr__(n, "statement", root_statement)
    return _case([d, n], evidence=evs, hyps=hyps or []), n


# The #656 turn-6 case frame: the two still-ACTIVE hypotheses whose statements
# the disjunction root OR-ed together.
_INCIDENT_HYPS = [
    _hyp("Transient network congestion"),
    _hyp("Resource contention on the backend"),
]


def test_restating_root_holds_at_inconclusive():
    # The #656 turn-6 shape: the "root cause" is a disjunction of the case's
    # two still-ACTIVE hypotheses restating the symptom — every token already
    # lives in the case frame (novelty ~0.11). One self-labeled causal support
    # must NOT validate it — it holds at INCONCLUSIVE (a live candidate
    # needing a real mechanism).
    case, root = _anchored_case(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors",
        hyps=_INCIDENT_HYPS,
    )
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE
    assert root.validation_method == ValidationMethod.NONE


def test_novel_disjunction_without_hypotheses_is_not_blocked():
    # The SAME disjunction root in a case with NO standing hypotheses carries
    # genuinely novel tokens (congestion/contention are posited causes the
    # frame doesn't contain), so the guard does not block it. The guard blocks
    # restatement of the case frame, not disjunction per se — arbitration of a
    # multi-cause root against its MECE siblings is a separate concern (#656).
    case, root = _anchored_case(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED


def test_verbatim_symptom_as_cause_blocked_even_without_hypotheses():
    # Zero-novelty restatement needs no hypothesis context to be blocked.
    case, root = _anchored_case("Intermittent 502 errors under load")
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE


def test_terse_subset_mechanism_passes():
    # Review-flagged false-positive class under the old similarity scoring: a
    # terse root fully lexically contained in a verbose anchor. Under the
    # novelty bar it passes when it carries any genuinely novel content.
    case, root = _anchored_case(
        "Upstream keepalive pool exhaustion",  # 'keepalive'/'pool'/'exhaustion' novel
    )
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED


def test_mechanism_root_validates_past_the_guard():
    # A root that adds explanatory depth (a mechanism, not a paraphrase)
    # validates exactly as before.
    case, root = _anchored_case(
        "Database connection pool max_size set below concurrent request demand"
    )
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED
    assert root.validation_method == ValidationMethod.EMPIRICAL


def test_intermediate_rung_may_paraphrase_the_problem():
    # ROOT-only scope: the rung adjacent to D legitimately paraphrases the
    # failure mode (the ladder converges on the problem) — the guard must not
    # block an INTERMEDIATE node.
    case, rung = _anchored_case(
        "Intermittent 502 errors on API requests under load",
        node_type=NodeType.INTERMEDIATE,
    )
    derive_node_states(case)
    assert rung.node_state == NodeState.VALIDATED


def test_restating_root_vs_symptom_statement_anchor():
    # The guard also anchors on problem_verification.symptom_statement (no
    # PROBLEM node needed) — _case sets symptom "X fails", so use a root that
    # restates a realistic symptom via a dedicated case.
    ev = _evidence("ev_causal", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(0xD2),
        node_type=NodeType.ROOT,
        links=[_link("ev_causal", EvidenceStance.SUPPORTS)],
    )
    object.__setattr__(root, "statement", "Orders fail intermittently during checkout")
    case = _case([root], evidence=[ev])
    case.problem_verification.symptom_statement = (
        "Checkout orders are failing intermittently"
    )
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE


def test_restatement_block_counted_once_per_event():
    # The calibration counter counts BLOCK EVENTS (state transitions), never
    # fixpoint passes or repeat derives of an already-held node: one stuck
    # symptom-as-cause root across many derives = ONE increment.
    from unittest.mock import patch

    case, root = _anchored_case(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors",
        hyps=_INCIDENT_HYPS,
    )
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "root_validation_blocked_restatement_total"
    ) as counter:
        derive_node_states(case)  # blocks: CANDIDATE -> INCONCLUSIVE (1 event)
        derive_node_states(case)  # already held: no new event
        derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE
    assert counter.inc.call_count == 1


def test_non_restating_validation_never_touches_the_counter():
    from unittest.mock import patch

    case, root = _anchored_case(
        "Database connection pool max_size set below concurrent request demand"
    )
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "root_validation_blocked_restatement_total"
    ) as counter:
        derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED
    assert counter.inc.call_count == 0


def test_attached_own_hypothesis_mirror_does_not_block():
    # The root's OWN attached hypothesis legitimately mirrors it (the normal
    # chain shape); its statement must not count toward the root's frame.
    case, root = _anchored_case(
        "Database connection pool max_size set below concurrent request demand"
    )
    own = _hyp(
        "Database connection pool max_size set below concurrent request demand",
        root_node_id=root.node_id,
    )
    case.hypotheses[own.hypothesis_id] = own
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED


def test_unattached_own_hypothesis_mirror_does_not_block():
    # Attachment lag (the common emission shape): the root's own hypothesis is
    # not yet linked (root_node_id=None) but MUTUALLY mirrors the root — it is
    # the presumptive owner and must not pollute the frame. (One-way containment
    # stays in the frame: the incident's disjunction root contains each sibling
    # but mirrors none, so the incident is still caught — see
    # test_restating_root_holds_at_inconclusive.)
    case, root = _anchored_case(
        "Database connection pool max_size set below concurrent request demand"
    )
    own = _hyp("Database connection pool max_size set below concurrent request demand")
    case.hypotheses[own.hypothesis_id] = own
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED


def test_validated_root_survives_later_paraphrasing_sibling():
    # ENTRY-bar monotonicity: a root that validly entered VALIDATED is ruled by
    # its evidence alone — a later sibling emission whose wording overlaps must
    # not retract the conclusion (the non-monotonic flap the entry semantics
    # exist to prevent).
    case, root = _anchored_case(
        "Database connection pool max_size set below concurrent request demand"
    )
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED
    # Two siblings arrive whose wording overlaps the root heavily.
    for stmt in (
        "Connection pool max_size below demand on the database",
        "Concurrent request demand exceeds the database connection pool size",
    ):
        h = _hyp(stmt)
        case.hypotheses[h.hypothesis_id] = h
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED  # evidence rules; no flap


# ---------------------------------------------------------------------------
# INV-29: §7.1 independent-support bar — a single self-labeled causal datum
# never validates a ROOT
# ---------------------------------------------------------------------------


def _single_support_root_case():
    """A ROOT with ONE causally-grounding SUPPORTS link (the pre-P1.3 shape)."""
    ev = _evidence("ev_only", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(0xE1),
        node_type=NodeType.ROOT,
        links=[_link("ev_only", EvidenceStance.SUPPORTS)],
    )
    return _case([root], evidence=[ev]), root


def test_single_causal_support_holds_root_at_inconclusive():
    # INV-29: one self-labeled causal datum is a live candidate, never a
    # validated conclusion (NO-COLLAPSE: held, not refuted).
    case, root = _single_support_root_case()
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE
    assert root.validation_method == ValidationMethod.NONE


def test_mirrored_causal_rows_collapse_to_one_observation():
    # Two rows re-recording the SAME observation (mutual-mirror contents) are
    # ONE independent support — re-emitting a datum cannot clear the bar.
    ev_a = _evidence("ev_dup_a", EvidenceCategory.CAUSAL_EVIDENCE)
    ev_b = _evidence("ev_dup_b", EvidenceCategory.CAUSAL_EVIDENCE)
    shared = "config diff shows pool max_size dropped from 100 to 5 at deploy"
    object.__setattr__(ev_a, "summary", shared)
    object.__setattr__(ev_b, "summary", shared + " window")
    root = _node(
        _nid(0xE2),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_dup_a", EvidenceStance.SUPPORTS),
            _link("ev_dup_b", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case([root], evidence=[ev_a, ev_b])
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE


def test_duplicate_links_to_same_evidence_count_once():
    # Per-evidence dedup: two SUPPORTS links referencing the SAME row are one
    # support, not two.
    ev = _evidence("ev_same", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(0xE3),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_same", EvidenceStance.SUPPORTS),
            _link("ev_same", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case([root], evidence=[ev])
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE


def test_self_hedged_support_is_not_causal_grounding():
    # A link the model itself marks doubtful (< CAUSAL_STANCE_CONFIDENCE_MIN)
    # does not count toward the bar...
    evs = [
        _evidence("ev_firm", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("ev_hedged", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    root = _node(
        _nid(0xE4),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_firm", EvidenceStance.SUPPORTS),
            _link("ev_hedged", EvidenceStance.SUPPORTS, stance_confidence=0.4),
        ],
    )
    case = _case([root], evidence=evs)
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE


def test_support_at_confidence_threshold_counts():
    # ...while a link AT the threshold does (>= bar, boundary pin).
    evs = [
        _evidence("ev_firm2", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("ev_at_bar", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    root = _node(
        _nid(0xE5),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_firm2", EvidenceStance.SUPPORTS),
            _link("ev_at_bar", EvidenceStance.SUPPORTS, stance_confidence=0.6),
        ],
    )
    case = _case([root], evidence=evs)
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED


def test_rung_keeps_single_support_bar():
    # Non-ROOT rungs validate on >=1 causal support — the conclusion-minting
    # bar is ROOT-only.
    ev = _evidence("ev_rung1", EvidenceCategory.CAUSAL_EVIDENCE)
    rung = _node(_nid(0xE6), links=[_link("ev_rung1", EvidenceStance.SUPPORTS)])
    case = _case([rung], evidence=[ev])
    derive_node_states(case)
    assert rung.node_state == NodeState.VALIDATED


def test_confirmed_root_satisfies_bar_with_single_support():
    # A counterfactually CONFIRMED root (engine-stamped causal_absence
    # SUPPORTS, M2 top grade) satisfies the bar outright — a confirmed
    # 1-support case recomputed post-RESOLVED must not demote.
    evs = [
        _evidence("ev_solo", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("ev_gone", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE),
    ]
    root = _node(
        _nid(0xE7),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_solo", EvidenceStance.SUPPORTS),
            _link("ev_gone", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case([root], evidence=evs)
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED


def test_support_count_block_counted_once_per_event():
    # Block-event semantics mirror the restatement counter: one stuck
    # under-supported root across many derives = ONE increment.
    from unittest.mock import patch

    case, root = _single_support_root_case()
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "root_validation_blocked_support_count_total"
    ) as counter:
        derive_node_states(case)  # blocks: CANDIDATE -> INCONCLUSIVE (1 event)
        derive_node_states(case)  # already held: no new event
        derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE
    assert counter.inc.call_count == 1


def test_support_block_requires_a_real_causal_link():
    # A root with only symptom-backed support was never causally supported —
    # that is not a P1.3 block event (metrics attribution).
    from unittest.mock import patch

    ev = _evidence("ev_sympt", EvidenceCategory.SYMPTOM_EVIDENCE)
    root = _node(
        _nid(0xE8),
        node_type=NodeType.ROOT,
        links=[_link("ev_sympt", EvidenceStance.SUPPORTS)],
    )
    case = _case([root], evidence=[ev])
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "root_validation_blocked_support_count_total"
    ) as counter:
        derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE
    assert counter.inc.call_count == 0


def test_two_independent_supports_never_touch_the_support_counter():
    from unittest.mock import patch

    case, root = _anchored_case(
        "Database connection pool max_size set below concurrent request demand"
    )
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "root_validation_blocked_support_count_total"
    ) as counter:
        derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED
    assert counter.inc.call_count == 0


def test_undersupported_restating_root_attributed_to_support_counter():
    # A root failing BOTH the support bar and the restatement guard is
    # attributed to the support counter; the restatement counter requires
    # would_validate (causal bar passed), so the two never double-count.
    from unittest.mock import patch

    d = _problem_node()
    object.__setattr__(d, "statement", _D_STATEMENT)
    ev = _evidence("ev_causal_one", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _node(
        _nid(0xE9),
        node_type=NodeType.ROOT,
        links=[_link("ev_causal_one", EvidenceStance.SUPPORTS)],
    )
    object.__setattr__(root, "statement", _D_STATEMENT)  # verbatim restatement
    case = _case([d, root], evidence=[ev])
    with (
        patch(
            "faultmaven.core.investigation.causal_graph."
            "root_validation_blocked_support_count_total"
        ) as support_counter,
        patch(
            "faultmaven.core.investigation.causal_graph."
            "root_validation_blocked_restatement_total"
        ) as restatement_counter,
    ):
        derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE
    assert support_counter.inc.call_count == 1
    assert restatement_counter.inc.call_count == 0


def test_zero_confidence_support_is_not_grounding():
    # Boundary pin for the None-vs-0.0 distinction: an EXPLICIT 0.0 is the
    # strongest possible self-hedge and must stay filtered (a naive `or`
    # coerces falsy 0.0 to the 1.0 default — the regression this pins).
    evs = [
        _evidence("ev_zfirm", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("ev_zzero", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    root = _node(
        _nid(0xEA),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_zfirm", EvidenceStance.SUPPORTS),
            _link("ev_zzero", EvidenceStance.SUPPORTS, stance_confidence=0.0),
        ],
    )
    case = _case([root], evidence=evs)
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE


def test_untokenizable_row_never_supplies_the_decisive_support():
    # A row whose content is all stopwords is unjudgeable and counts ZERO
    # toward independence — it must not be the second observation.
    ev_real = _evidence("ev_real1", EvidenceCategory.CAUSAL_EVIDENCE)
    ev_vacuous = _evidence("ev_vac1", EvidenceCategory.CAUSAL_EVIDENCE)
    object.__setattr__(ev_vacuous, "summary", "it is as was")
    root = _node(
        _nid(0xEB),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_real1", EvidenceStance.SUPPORTS),
            _link("ev_vac1", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case([root], evidence=[ev_real, ev_vacuous])
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE


def test_two_mirrored_plus_one_distinct_validates():
    # Passing direction of the mirror collapse: three rows where two are the
    # same observation re-worded still yield TWO independent observations.
    ev_a = _evidence("ev_ma", EvidenceCategory.CAUSAL_EVIDENCE)
    ev_b = _evidence("ev_mb", EvidenceCategory.CAUSAL_EVIDENCE)
    ev_c = _evidence("ev_mc", EvidenceCategory.CAUSAL_EVIDENCE)
    object.__setattr__(
        ev_a, "summary", "config diff shows pool max_size dropped from 100 to 5"
    )
    object.__setattr__(
        ev_b, "summary", "config diff shows pool max_size dropped from 100 to 5 window"
    )
    object.__setattr__(
        ev_c, "summary", "db wait queue saturation logged at incident start"
    )
    root = _node(
        _nid(0xEC),
        node_type=NodeType.ROOT,
        links=[
            _link("ev_ma", EvidenceStance.SUPPORTS),
            _link("ev_mb", EvidenceStance.SUPPORTS),
            _link("ev_mc", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case([root], evidence=[ev_a, ev_b, ev_c])
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED


def test_rung_with_only_hedged_link_does_not_validate():
    # The confidence filter applies to rungs too: a rung whose only causal
    # link is self-hedged stays INCONCLUSIVE.
    ev = _evidence("ev_rhedge", EvidenceCategory.CAUSAL_EVIDENCE)
    rung = _node(
        _nid(0xED),
        links=[_link("ev_rhedge", EvidenceStance.SUPPORTS, stance_confidence=0.4)],
    )
    case = _case([rung], evidence=[ev])
    derive_node_states(case)
    assert rung.node_state == NodeState.INCONCLUSIVE


def test_llm_absence_supports_via_ingest_never_completes_the_bar():
    # Composition pin (INV-28 x INV-29): the confirmed-root bypass leans on
    # the ingest strip — an LLM-emitted SUPPORTS-on-absence run through
    # ingest_emitted_chain must NOT reach the node, so a 1-support root stays
    # INCONCLUSIVE (the bypass is engine-stamp-only).
    from types import SimpleNamespace

    from faultmaven.core.investigation.causal_graph import ingest_emitted_chain

    ev = _evidence("ev_one1", EvidenceCategory.CAUSAL_EVIDENCE)
    absence = _evidence("ev_gone1", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    root = _node(
        _nid(0xEE),
        node_type=NodeType.ROOT,
        links=[_link("ev_one1", EvidenceStance.SUPPORTS)],
    )
    case = _case([root], evidence=[ev, absence])
    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref=root.node_id,
                evidence_id=absence.evidence_id,
                stance="supports",
                reasoning="it went away so this was the cause",
            )
        ],
        current_turn=5,
    )
    derive_node_states(case)
    assert root.node_state == NodeState.INCONCLUSIVE
