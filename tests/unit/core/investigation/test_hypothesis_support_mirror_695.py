"""#695 Defect B (node-axis wiring): the flat ``hypothesis_evidence`` axis and
the ``causal_node_evidence`` axis were disjoint, so a hypothesis grounded on the
flat axis left its chain ROOT node with zero causal support — uncertifiable by
``derive_node_states`` / ``grade_cause_assurance`` / the runbook gate.

B1 mirrors a hypothesis's causal SUPPORTS links onto its root node.
B2 resolves the RCC's ``names_root_node_id`` same-turn ``new_index_N`` placeholder
so Tier-1 RCC->hypothesis attribution can match a real node id.

These pin the wiring in isolation and end-to-end (NO_ROOT -> MECHANISTIC with a
resolved ``validated_hypothesis_id`` on a clean, non-fragmented solved case).
"""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import (
    derive_node_states,
    link_llm_rcc_to_cause,
    mirror_hypothesis_support_to_root_nodes,
)
from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    grade_cause_assurance,
)
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    RootCauseConclusion,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _nid(seed: int) -> str:
    return f"cn_{seed:012x}"


def _evidence(label: str, category: EvidenceCategory) -> Evidence:
    # Distinct summaries so two rows read as INDEPENDENT causal supports under the
    # INV-29 mirror collapse (identical summaries would count as one).
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


def _root(node_id: str, statement: str = "client certificate expired") -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[],
        generated_at_turn=1,
    )


def _hyp(
    root_node_id: str | None, *, hlinks=None, statement="client certificate expired"
) -> Hypothesis:
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.CONFIG,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=1,
        rationale="r",
        state=HypothesisState.ACTIVE,
        root_node_id=root_node_id,
        evidence_links=hlinks or [],
    )


def _hlink(
    label: str, stance: EvidenceStance, conf: float = 1.0, hyp_id: str = "hyp"
) -> HypothesisEvidenceLink:
    return HypothesisEvidenceLink(
        hypothesis_id=hyp_id,
        evidence_id=_eid(label),
        stance=stance,
        reasoning="bears on the cause",
        stance_confidence=conf,
    )


def _case(*, nodes=None, evidence=None, hyps=None) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="mTLS handshake fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="mTLS handshake fails", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = 4
    case.causal_nodes = {n.node_id: n for n in (nodes or [])}
    case.evidence = evidence or []
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    return case


def _node_ev_ids(node) -> set:
    return {el.evidence_id for el in node.evidence_links}


# --------------------------------------------------------------------------- #
# B1 — mirror_hypothesis_support_to_root_nodes (pure)
# --------------------------------------------------------------------------- #


def test_b1_mirrors_causal_support_onto_root_node():
    root = _root(_nid(1))
    evs = [
        _evidence("c1", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("c2", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    hyp = _hyp(
        root.node_id,
        hlinks=[
            _hlink("c1", EvidenceStance.SUPPORTS),
            _hlink("c2", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case(nodes=[root], evidence=evs, hyps=[hyp])
    assert _node_ev_ids(root) == set()  # disjoint axis: node starts empty

    n = mirror_hypothesis_support_to_root_nodes(case, case.current_turn)

    assert n == 2
    assert _node_ev_ids(root) == {_eid("c1"), _eid("c2")}
    assert all(el.stance == EvidenceStance.SUPPORTS for el in root.evidence_links)


def test_b1_mirrored_links_let_derive_validate_the_root():
    """The whole point: two mirrored independent causal supports carry the root
    through the §7.1 ROOT bar in ``derive_node_states``."""
    root = _root(_nid(2))
    evs = [
        _evidence("c1", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("c2", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    hyp = _hyp(
        root.node_id,
        hlinks=[
            _hlink("c1", EvidenceStance.SUPPORTS),
            _hlink("c2", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case(nodes=[root], evidence=evs, hyps=[hyp])
    mirror_hypothesis_support_to_root_nodes(case, case.current_turn)
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED
    # Validated but not counterfactually confirmed -> MECHANISTIC (not NO_ROOT).
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


def test_b1_skips_symptom_and_absence_rows():
    """Only CAUSAL_EVIDENCE mirrors. Symptom rows bear on D; a SUPPORTS on an
    absence row is engine-reserved (counterfactual mint) and must never be
    created here."""
    root = _root(_nid(3))
    evs = [
        _evidence("s1", EvidenceCategory.SYMPTOM_EVIDENCE),
        _evidence("a1", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE),
    ]
    hyp = _hyp(
        root.node_id,
        hlinks=[
            _hlink("s1", EvidenceStance.SUPPORTS),
            _hlink("a1", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case(nodes=[root], evidence=evs, hyps=[hyp])
    n = mirror_hypothesis_support_to_root_nodes(case, case.current_turn)
    assert n == 0
    assert _node_ev_ids(root) == set()


def test_b1_skips_refutes_stance():
    root = _root(_nid(4))
    evs = [_evidence("c1", EvidenceCategory.CAUSAL_EVIDENCE)]
    hyp = _hyp(root.node_id, hlinks=[_hlink("c1", EvidenceStance.REFUTES)])
    case = _case(nodes=[root], evidence=evs, hyps=[hyp])
    assert mirror_hypothesis_support_to_root_nodes(case, case.current_turn) == 0
    assert _node_ev_ids(root) == set()


def test_b1_does_not_overwrite_explicit_node_link():
    """An explicit node_evidence emission wins: the mirror only FILLS a missing
    link, never clobbers the LLM's own node-level assessment (e.g. a hedge)."""
    root = _root(_nid(5))
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=_eid("c1"),
            stance=EvidenceStance.SUPPORTS,
            reasoning="explicit node emission",
            stance_confidence=0.42,
            linked_at_turn=2,
        )
    )
    evs = [_evidence("c1", EvidenceCategory.CAUSAL_EVIDENCE)]
    hyp = _hyp(root.node_id, hlinks=[_hlink("c1", EvidenceStance.SUPPORTS, conf=1.0)])
    case = _case(nodes=[root], evidence=evs, hyps=[hyp])
    assert mirror_hypothesis_support_to_root_nodes(case, case.current_turn) == 0
    assert len(root.evidence_links) == 1
    assert root.evidence_links[0].stance_confidence == 0.42  # untouched


def test_b1_is_idempotent():
    root = _root(_nid(6))
    evs = [_evidence("c1", EvidenceCategory.CAUSAL_EVIDENCE)]
    hyp = _hyp(root.node_id, hlinks=[_hlink("c1", EvidenceStance.SUPPORTS)])
    case = _case(nodes=[root], evidence=evs, hyps=[hyp])
    assert mirror_hypothesis_support_to_root_nodes(case, case.current_turn) == 1
    assert mirror_hypothesis_support_to_root_nodes(case, case.current_turn) == 0
    assert len(root.evidence_links) == 1


def test_b1_skips_hypothesis_with_no_root():
    evs = [_evidence("c1", EvidenceCategory.CAUSAL_EVIDENCE)]
    hyp = _hyp(None, hlinks=[_hlink("c1", EvidenceStance.SUPPORTS)])
    case = _case(nodes=[], evidence=evs, hyps=[hyp])
    assert mirror_hypothesis_support_to_root_nodes(case, case.current_turn) == 0


def test_b1_carries_stance_confidence_from_hypothesis_link():
    root = _root(_nid(7))
    evs = [_evidence("c1", EvidenceCategory.CAUSAL_EVIDENCE)]
    hyp = _hyp(root.node_id, hlinks=[_hlink("c1", EvidenceStance.SUPPORTS, conf=0.73)])
    case = _case(nodes=[root], evidence=evs, hyps=[hyp])
    mirror_hypothesis_support_to_root_nodes(case, case.current_turn)
    assert root.evidence_links[0].stance_confidence == 0.73


# --------------------------------------------------------------------------- #
# B1/B2 — via _apply_chain_emission (engine wiring)
# --------------------------------------------------------------------------- #


def _engine() -> MilestoneEngine:
    # _apply_chain_emission uses only module functions + the args; no deps needed.
    return MilestoneEngine.__new__(MilestoneEngine)


def test_apply_chain_emission_mirrors_support_to_the_new_root():
    """End-to-end through the engine: a hypothesis carries flat causal supports;
    the chain emits its root this turn; after ingest the supports appear on the
    node and the root validates."""
    eng = _engine()
    evs = [
        _evidence("c1", EvidenceCategory.CAUSAL_EVIDENCE),
        _evidence("c2", EvidenceCategory.CAUSAL_EVIDENCE),
    ]
    hyp = _hyp(
        None,  # not yet rooted — the chain roots it this turn
        hlinks=[
            _hlink("c1", EvidenceStance.SUPPORTS),
            _hlink("c2", EvidenceStance.SUPPORTS),
        ],
    )
    case = _case(evidence=evs, hyps=[hyp])
    metadata = {
        "hypotheses_generated": [hyp.hypothesis_id],
        "hyp_root_refs": {hyp.hypothesis_id: "new_index_0"},
    }
    updates = SimpleNamespace(
        causal_nodes_to_add=[
            SimpleNamespace(
                statement="client certificate expired",
                node_type=NodeType.ROOT,
                produces="D",
                and_group=None,
            )
        ],
        causal_edges_to_add=[],
        node_evidence_links=[],
    )

    eng._apply_chain_emission(case, updates, metadata)

    assert hyp.root_node_id is not None
    root = case.causal_nodes[hyp.root_node_id]
    assert _node_ev_ids(root) == {_eid("c1"), _eid("c2")}
    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED


def test_b2_resolves_names_root_node_id_placeholder():
    """The RCC names its cause's root as a same-turn ``new_index_0``; B2 resolves
    it to the real node id so Tier-1 attribution can match."""
    eng = _engine()
    hyp = _hyp(None, statement="client certificate expired")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="client certificate expired",
        mechanism="expired cert rejected at handshake",
        evidence_basis=[],
        likelihood=1.0,
        confidence_level=ConfidenceLevel.VERIFIED,
        names_root_node_id="new_index_0",
    )
    metadata = {
        "hypotheses_generated": [hyp.hypothesis_id],
        "hyp_root_refs": {hyp.hypothesis_id: "new_index_0"},
        "rcc_authored_this_turn": True,
    }
    updates = SimpleNamespace(
        causal_nodes_to_add=[
            SimpleNamespace(
                statement="client certificate expired",
                node_type=NodeType.ROOT,
                produces="D",
                and_group=None,
            )
        ],
        causal_edges_to_add=[],
        node_evidence_links=[],
    )

    eng._apply_chain_emission(case, updates, metadata)

    resolved = case.root_cause_conclusion.names_root_node_id
    assert resolved is not None
    assert resolved.startswith("cn_")
    assert resolved == hyp.root_node_id
    # Tier-1 RCC->hypothesis attribution now matches the real node id.
    assert link_llm_rcc_to_cause(case) is True
    assert case.root_cause_conclusion.validated_hypothesis_id == hyp.hypothesis_id


def test_b2_leaves_real_node_id_untouched():
    eng = _engine()
    root = _root(_nid(9))
    hyp = _hyp(root.node_id, statement="client certificate expired")
    case = _case(nodes=[root], hyps=[hyp])
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="client certificate expired",
        mechanism="m",
        evidence_basis=[],
        likelihood=1.0,
        confidence_level=ConfidenceLevel.VERIFIED,
        names_root_node_id=root.node_id,  # already a real cn_ id
    )
    metadata = {"rcc_authored_this_turn": True, "hyp_root_refs": {}}
    updates = SimpleNamespace(
        causal_nodes_to_add=[], causal_edges_to_add=[], node_evidence_links=[]
    )
    eng._apply_chain_emission(case, updates, metadata)
    assert case.root_cause_conclusion.names_root_node_id == root.node_id


def test_b2_does_not_resolve_placeholder_from_a_prior_turn():
    """The placeholder indexes THIS turn's emission; when the RCC was not authored
    this turn there is no matching ``created`` list, so B2 leaves it alone rather
    than mis-resolving against an unrelated emission."""
    eng = _engine()
    hyp = _hyp(None)
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="client certificate expired",
        mechanism="m",
        evidence_basis=[],
        likelihood=1.0,
        confidence_level=ConfidenceLevel.VERIFIED,
        names_root_node_id="new_index_0",
    )
    metadata = {
        "rcc_authored_this_turn": False,
        "hyp_root_refs": {hyp.hypothesis_id: "new_index_0"},
    }
    updates = SimpleNamespace(
        causal_nodes_to_add=[
            SimpleNamespace(
                statement="client certificate expired",
                node_type=NodeType.ROOT,
                produces="D",
                and_group=None,
            )
        ],
        causal_edges_to_add=[],
        node_evidence_links=[],
    )
    eng._apply_chain_emission(case, updates, metadata)
    assert case.root_cause_conclusion.names_root_node_id == "new_index_0"
