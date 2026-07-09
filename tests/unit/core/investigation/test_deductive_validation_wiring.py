"""Deductive validation wiring (#593) — proof-by-exclusion stamps DEDUCTIVE.

Covers the two-part wiring that turns the deductive grounding arm from
designed-but-dead into a firing path (§7.1.1):

  1. ``validate_by_exclusion`` stamps ``validation_method=DEDUCTIVE`` on a
     ROOT survivor the LLM certified exhaustive, but ONLY when the engine's own
     guards hold (≥2 members, all-but-survivor ABSOLUTELY refuted) — the agent
     supplies exhaustiveness (guard #1); the engine checks everything computable.
  2. ``derive_node_states`` drives a sibling's ``belief`` to 0 on a COUNTERFACTUAL
     (absence-based) refutation, making guard #3's ``belief <= 0.05`` absolute-
     exclusion bar reachable — a merely-correlational net-refute stays above the
     bar and blocks the deduction (graceful denial).

Acceptance: a case validated this way grades ``MECHANISTIC`` (validated but not
counterfactually confirmed — the §7 harvest bar is ``CONFIRMED``, M2).
Pure graph/grade primitives — no LLM, no DB.
"""

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import (
    DEDUCTIVE_EXCLUSION_MAX_BELIEF,
    derive_node_states,
    validate_by_exclusion,
)
from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    grade_cause_assurance,
)
from faultmaven.core.investigation.milestone_engine import (
    _recompute_cause_state_from_chain,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
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


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _node(
    node_id,
    *,
    node_type=NodeType.INTERMEDIATE,
    state=NodeState.CANDIDATE,
    belief=0.5,
    method=ValidationMethod.NONE,
    refutation_reason=None,
    links=None,
) -> CausalNode:
    if state == NodeState.REFUTED and not refutation_reason:
        refutation_reason = "refuted in test"
    return CausalNode(
        node_id=node_id,
        statement=f"node {node_id}",
        node_type=node_type,
        node_state=state,
        validation_method=method,
        belief=belief,
        actionable=(state == NodeState.VALIDATED and node_type == NodeType.ROOT),
        refutation_reason=refutation_reason,
        evidence_links=links or [],
        generated_at_turn=1,
    )


def _edge(cause, effect, and_group=None) -> CausalEdge:
    return CausalEdge(cause_node_id=cause, effect_node_id=effect, and_group=and_group)


def _link(label, stance) -> NodeEvidenceLink:
    return NodeEvidenceLink(
        evidence_id=_eid(label),
        stance=stance,
        reasoning="bears on rung",
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
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="intermittent latency",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="intermittent latency", severity=CaseSeverity.HIGH
        ),
    )
    case.causal_nodes = {n.node_id: n for n in nodes}
    case.causal_edges = edges or []
    case.evidence = evidence or []
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    return case


def _problem() -> CausalNode:
    return CausalNode(
        node_id=_nid(0xD),
        statement="D: intermittent latency",
        node_type=NodeType.PROBLEM,
        generated_at_turn=1,
    )


def _two_root_differential(*, sibling_belief: float, sibling_state=NodeState.REFUTED):
    """A survivor ROOT (unobservable, CANDIDATE) and one sibling ROOT, both OR-
    alternatives producing D. Returns (case, survivor_id, sibling_id)."""
    d = _problem()
    survivor = _node(_nid(1), node_type=NodeType.ROOT, state=NodeState.CANDIDATE)
    sibling = _node(
        _nid(2),
        node_type=NodeType.ROOT,
        state=sibling_state,
        belief=sibling_belief,
        refutation_reason=(
            "counterfactually refuted" if sibling_state == NodeState.REFUTED else None
        ),
    )
    edges = [_edge(survivor.node_id, d.node_id), _edge(sibling.node_id, d.node_id)]
    case = _case([d, survivor, sibling], edges=edges)
    return case, survivor.node_id, sibling.node_id


# ---------------------------------------------------------------------------
# Part 2 — belief strength (the engine-computable half of guard #3)
# ---------------------------------------------------------------------------


def test_counterfactual_refute_drives_belief_to_zero():
    """An absence-based (CAUSAL_ABSENCE) REFUTES is an ABSOLUTE exclusion: derive
    drops belief to 0 so proof-by-exclusion may count the sibling."""
    ev = _evidence("cf", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    n = _node(
        _nid(7), node_type=NodeType.ROOT, links=[_link("cf", EvidenceStance.REFUTES)]
    )
    case = _case([n], evidence=[ev])
    derive_node_states(case)
    assert n.node_state == NodeState.REFUTED
    assert n.belief == 0.0
    assert n.belief <= DEDUCTIVE_EXCLUSION_MAX_BELIEF


def test_correlational_refute_leaves_belief_above_bar():
    """A merely-correlational net-refute (CAUSAL_EVIDENCE REFUTES, no counterfactual)
    keeps belief above the exclusion cap, so it does NOT count as absolute — it
    blocks the deduction (graceful denial)."""
    ev = _evidence("corr", EvidenceCategory.CAUSAL_EVIDENCE)
    n = _node(
        _nid(8), node_type=NodeType.ROOT, links=[_link("corr", EvidenceStance.REFUTES)]
    )
    case = _case([n], evidence=[ev])
    derive_node_states(case)
    assert n.node_state == NodeState.REFUTED
    assert n.belief > DEDUCTIVE_EXCLUSION_MAX_BELIEF  # unchanged from default 0.5


# ---------------------------------------------------------------------------
# Part 1 — validate_by_exclusion stamping + the acceptance grade
# ---------------------------------------------------------------------------


def test_exhausted_and_asserted_differential_stamps_deductive():
    case, survivor_id, _ = _two_root_differential(sibling_belief=0.0)
    changed = validate_by_exclusion(case, {survivor_id})
    survivor = case.causal_nodes[survivor_id]
    assert changed is True
    assert survivor.node_state == NodeState.VALIDATED
    assert survivor.validation_method == ValidationMethod.DEDUCTIVE
    assert survivor.actionable is True  # M1
    # Acceptance: a deductively validated root is MECHANISTIC grade — validated
    # (unlocks treatment) but not counterfactually confirmed (M2: even a
    # deductive proof rests on model-mediated refutations, so "verified" and
    # harvest wait for the gone⇒gone confirmation).
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


def test_graceful_denial_when_sibling_only_correlationally_refuted():
    """Sibling REFUTED but belief above the cap (not absolute) → deduction blocked;
    survivor stays CANDIDATE (NO COLLAPSE — keep investigating)."""
    case, survivor_id, _ = _two_root_differential(sibling_belief=0.5)
    changed = validate_by_exclusion(case, {survivor_id})
    assert changed is False
    assert case.causal_nodes[survivor_id].node_state == NodeState.CANDIDATE
    assert grade_cause_assurance(case) == CauseAssuranceGrade.NO_ROOT


def test_graceful_denial_when_survivor_not_asserted():
    """The differential has collapsed, but the LLM never certified exhaustiveness →
    the engine does NOT self-certify; nothing is stamped."""
    case, survivor_id, _ = _two_root_differential(sibling_belief=0.0)
    changed = validate_by_exclusion(case, set())  # no assertion
    assert changed is False
    assert case.causal_nodes[survivor_id].node_state == NodeState.CANDIDATE


def test_graceful_denial_single_member_or_set():
    """One survivor, no alternative refuted — exclusion has learned nothing (N<2)."""
    d = _problem()
    survivor = _node(_nid(1), node_type=NodeType.ROOT, state=NodeState.CANDIDATE)
    case = _case([d, survivor], edges=[_edge(survivor.node_id, d.node_id)])
    changed = validate_by_exclusion(case, {survivor.node_id})
    assert changed is False
    assert case.causal_nodes[survivor.node_id].node_state == NodeState.CANDIDATE


def test_asserted_non_root_is_ignored():
    """Only a ROOT cause is validated by exclusion; an asserted intermediate is skipped."""
    d = _problem()
    inter = _node(_nid(3), node_type=NodeType.INTERMEDIATE, state=NodeState.CANDIDATE)
    sibling = _node(
        _nid(2), node_type=NodeType.INTERMEDIATE, state=NodeState.REFUTED, belief=0.0
    )
    edges = [_edge(inter.node_id, d.node_id), _edge(sibling.node_id, d.node_id)]
    case = _case([d, inter, sibling], edges=edges)
    changed = validate_by_exclusion(case, {inter.node_id})
    assert changed is False
    assert case.causal_nodes[inter.node_id].node_state == NodeState.CANDIDATE


def test_already_refuted_survivor_not_resurrected():
    """An assertion never re-opens a REFUTED survivor."""
    case, survivor_id, _ = _two_root_differential(sibling_belief=0.0)
    survivor = case.causal_nodes[survivor_id]
    survivor.node_state = NodeState.REFUTED
    survivor.validation_method = ValidationMethod.NONE
    survivor.refutation_reason = "already refuted"
    changed = validate_by_exclusion(case, {survivor_id})
    assert changed is False
    assert survivor.node_state == NodeState.REFUTED


# ---------------------------------------------------------------------------
# End-to-end: the two parts compose (derive drives belief → exclusion fires)
# ---------------------------------------------------------------------------


def test_derive_then_exclusion_composes():
    """Full path: a sibling counterfactually refuted by evidence reaches belief 0
    via derive_node_states, THEN validate_by_exclusion validates the survivor."""
    d = _problem()
    survivor = _node(_nid(1), node_type=NodeType.ROOT, state=NodeState.CANDIDATE)
    sibling = _node(
        _nid(2),
        node_type=NodeType.ROOT,
        state=NodeState.CANDIDATE,
        links=[_link("cf", EvidenceStance.REFUTES)],
    )
    ev = _evidence("cf", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    edges = [_edge(survivor.node_id, d.node_id), _edge(sibling.node_id, d.node_id)]
    case = _case([d, survivor, sibling], edges=edges, evidence=[ev])

    derive_node_states(case)  # sibling → REFUTED, belief → 0
    assert case.causal_nodes[sibling.node_id].node_state == NodeState.REFUTED
    assert case.causal_nodes[sibling.node_id].belief == 0.0

    changed = validate_by_exclusion(case, {survivor.node_id})
    assert changed is True
    assert (
        case.causal_nodes[survivor.node_id].validation_method
        == ValidationMethod.DEDUCTIVE
    )


# ---------------------------------------------------------------------------
# Engine threading: exclusion_survivors flows through the recompute + promotes
# cause_state to IDENTIFIED in the same pass.
# ---------------------------------------------------------------------------


def _hyp(root_node_id) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="hyp_000000000001",
        statement="a race condition",
        category=HypothesisCategory.OTHER,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="unobservable, reached by exclusion",
        root_node_id=root_node_id,
        generated_at_turn=1,
    )


def _differential_via_evidence():
    """A survivor ROOT (unobservable) + a sibling ROOT counterfactually refuted by
    DURABLE evidence, so ``derive_node_states`` (re-run each turn) keeps the sibling
    REFUTED + belief 0. Returns (case, survivor_id). Unlike ``_two_root_differential``
    this survives a derive pass — required for the recompute path."""
    d = _problem()
    survivor = _node(_nid(1), node_type=NodeType.ROOT, state=NodeState.CANDIDATE)
    sibling = _node(
        _nid(2),
        node_type=NodeType.ROOT,
        state=NodeState.CANDIDATE,
        links=[_link("cf", EvidenceStance.REFUTES)],
    )
    ev = _evidence("cf", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    edges = [_edge(survivor.node_id, d.node_id), _edge(sibling.node_id, d.node_id)]
    case = _case([d, survivor, sibling], edges=edges, evidence=[ev])
    case.hypotheses = {"hyp_000000000001": _hyp(survivor.node_id)}
    case.progress.symptom_verified = True  # cause-identification anchor
    return case, survivor.node_id


def test_recompute_threads_assertion_and_identifies_cause():
    case, survivor_id = _differential_via_evidence()

    _recompute_cause_state_from_chain(case, exclusion_survivors={survivor_id})

    assert (
        case.causal_nodes[survivor_id].validation_method == ValidationMethod.DEDUCTIVE
    )
    assert case.progress.cause_state == CauseState.IDENTIFIED


def test_recompute_without_assertion_leaves_cause_unidentified():
    case, survivor_id = _differential_via_evidence()

    _recompute_cause_state_from_chain(case)  # no exclusion_survivors

    assert case.causal_nodes[survivor_id].node_state == NodeState.CANDIDATE
    assert case.progress.cause_state != CauseState.IDENTIFIED


def _and_gate_case():
    """A survivor ROOT S validated by exclusion (OR-differential S vs S2→M), that
    is ALSO an AND-member of a downstream effect E (AND{S, J}→D) with its own causal
    support. Before S is stamped, E's AND-gate is unsatisfied (S is CANDIDATE); once
    S is deductively validated, E should validate — but only if derivation RE-RUNS
    after the stamp. Returns (case, S_id, E_id)."""
    d = _problem()
    s = _node(_nid(1), node_type=NodeType.ROOT, state=NodeState.CANDIDATE)
    s2 = _node(
        _nid(2),
        node_type=NodeType.ROOT,
        state=NodeState.CANDIDATE,
        links=[
            _link("cf_s2", EvidenceStance.REFUTES)
        ],  # counterfactual → REFUTED, belief 0
    )
    m = _node(_nid(3), node_type=NodeType.INTERMEDIATE, state=NodeState.CANDIDATE)
    j = _node(
        _nid(4),
        node_type=NodeType.INTERMEDIATE,
        state=NodeState.CANDIDATE,
        links=[
            _link("cs_j", EvidenceStance.SUPPORTS)
        ],  # own causal support → VALIDATED
    )
    e = _node(
        _nid(5),
        node_type=NodeType.INTERMEDIATE,
        state=NodeState.CANDIDATE,
        links=[_link("cs_e", EvidenceStance.SUPPORTS)],  # own causal support
    )
    evidence = [
        _evidence("cf_s2", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE),
        _evidence("cs_j", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("cs_e", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    edges = [
        _edge(s.node_id, m.node_id),  # OR-differential for M: S vs S2
        _edge(s2.node_id, m.node_id),
        _edge(m.node_id, d.node_id),
        _edge(s.node_id, e.node_id, and_group="g1"),  # AND-gate for E: {S, J}
        _edge(j.node_id, e.node_id, and_group="g1"),
        _edge(e.node_id, d.node_id),
    ]
    case = _case([d, s, s2, m, j, e], edges=edges, evidence=evidence)
    return case, s.node_id, e.node_id


def test_deductive_root_unlocks_downstream_and_gate_same_turn():
    """The recompute re-derives after a deductive stamp, so a DEDUCTIVE root
    satisfies its downstream AND-gate in the SAME turn (finding-2 fix)."""
    case, s_id, e_id = _and_gate_case()

    _recompute_cause_state_from_chain(case, exclusion_survivors={s_id})

    assert case.causal_nodes[s_id].validation_method == ValidationMethod.DEDUCTIVE
    # E's AND-gate {S, J} is now satisfied and E has its own causal support → VALIDATED.
    assert case.causal_nodes[e_id].node_state == NodeState.VALIDATED


def test_single_pass_without_rederive_leaves_and_gate_pending():
    """Contrast: a lone derive + validate_by_exclusion (no re-derive) stamps S but
    leaves E's AND-gate unsatisfied — demonstrating the re-derive is load-bearing."""
    case, s_id, e_id = _and_gate_case()

    derive_node_states(
        case
    )  # empirical pass — S still CANDIDATE, so E's AND-gate fails
    changed = validate_by_exclusion(
        case, {s_id}
    )  # stamps S DEDUCTIVE, but no re-derive

    assert changed is True
    assert case.causal_nodes[s_id].validation_method == ValidationMethod.DEDUCTIVE
    # Without the re-derive, E never re-evaluates its AND-gate this turn.
    assert case.causal_nodes[e_id].node_state == NodeState.INCONCLUSIVE


def test_exclusion_refuses_a_restating_survivor():
    """§7.1 restatement guard in the deductive lane: a survivor whose statement
    restates the problem anchor has excluded its alternatives without stating a
    mechanism — stamping it would conclude 'the problem causes itself'. The
    survivor stays un-stamped (graceful denial), so no lane can validate a
    restatement and the IDENTIFIED-without-conclusion split state is
    unreachable for fresh cases."""
    case, survivor_id, _ = _two_root_differential(sibling_belief=0.0)
    survivor = case.causal_nodes[survivor_id]
    # Restate the PROBLEM anchor ("D: intermittent latency").
    object.__setattr__(survivor, "statement", "intermittent latency on D")
    changed = validate_by_exclusion(case, {survivor_id})
    assert changed is False
    assert survivor.node_state == NodeState.CANDIDATE
    assert grade_cause_assurance(case) == CauseAssuranceGrade.NO_ROOT


def test_exclusion_stamps_a_mechanism_survivor_control():
    """Control: the identical differential with a mechanism-stating survivor
    stamps DEDUCTIVE exactly as before."""
    case, survivor_id, _ = _two_root_differential(sibling_belief=0.0)
    survivor = case.causal_nodes[survivor_id]
    object.__setattr__(
        survivor, "statement", "kernel conntrack table saturation drops packets"
    )
    assert validate_by_exclusion(case, {survivor_id}) is True
    assert survivor.node_state == NodeState.VALIDATED
    assert survivor.validation_method == ValidationMethod.DEDUCTIVE
