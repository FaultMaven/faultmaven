"""Gate 1b slice 2: chain-derived cause_state (Option A, §9.2) + M6 via Option (c).

When ``enable_hypothesis_chain_emission`` is ON, ``cause_state=IDENTIFIED`` is
derived from a VALIDATED chain root (real rung evidence), not flat assertion, and
failed-treatment demotion is made durable by attaching an engine REFUTES fact to
the root — so ``derive_node_states`` keeps it refuted across turns (no turn-28
resurrection). The flat path (flag OFF) is unchanged.
"""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation import milestone_engine
from faultmaven.core.investigation.causal_graph import (
    _attach_engine_refutation,
    any_chain_root_validated,
    demote_disconfirmed_cause_via_evidence,
    seed_problem_node,
)
from faultmaven.core.investigation.milestone_engine import (
    _recompute_assessment_state,
    _recompute_cause_state_from_chain,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    CauseState,
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


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(label, category=EvidenceCategory.CAUSAL_EVIDENCE) -> Evidence:
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


def _root(node_id="cn_000000000001", *, support_label=None) -> CausalNode:
    links = []
    if support_label:
        links = [
            NodeEvidenceLink(
                evidence_id=_eid(support_label),
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the root",
                linked_at_turn=2,
            )
        ]
    return CausalNode(
        node_id=node_id,
        statement="the root cause",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=links,
        generated_at_turn=1,
    )


def _hyp(
    root_node_id, *, refutes=0, supports=0, state=HypothesisState.ACTIVE
) -> Hypothesis:
    links = [
        HypothesisEvidenceLink(
            hypothesis_id="hyp_000000000001",
            evidence_id=_eid(f"r{i}"),
            stance=EvidenceStance.REFUTES,
            reasoning="contra",
            stance_confidence=0.9,
        )
        for i in range(refutes)
    ] + [
        HypothesisEvidenceLink(
            hypothesis_id="hyp_000000000001",
            evidence_id=_eid(f"s{i}"),
            stance=EvidenceStance.SUPPORTS,
            reasoning="pro",
            stance_confidence=0.9,
        )
        for i in range(supports)
    ]
    return Hypothesis(
        hypothesis_id="hyp_000000000001",
        statement="connection pool exhausted",
        category=HypothesisCategory.DATABASE,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root_node_id,
        evidence_links=links,
        generated_at_turn=1,
    )


def _case(nodes=None, edges=None, evidence=None, hyps=None) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="orders failing",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="orders failing", severity=CaseSeverity.HIGH
        ),
    )
    case.causal_nodes = {n.node_id: n for n in (nodes or [])}
    case.causal_edges = edges or []
    case.evidence = evidence or []
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    return case


def _chain_case():
    """A case with a root→D chain and a CAUSAL_EVIDENCE SUPPORTS on the root."""
    root = _root(support_label="ev_root_support")
    case = _case(nodes=[root], evidence=[_evidence("ev_root_support")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=d.node_id)
    ]
    hyp = _hyp(root.node_id)
    case.hypotheses = {hyp.hypothesis_id: hyp}
    return case, root, hyp


# ---------------------------------------------------------------------------
# any_chain_root_validated (§9.2 signal)
# ---------------------------------------------------------------------------


def test_validated_root_grounds_identified():
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)
    assert root.node_state == NodeState.VALIDATED
    assert any_chain_root_validated(case) is True
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.progress.root_cause_likelihood > 0  # invariant floored
    assert case.progress.root_cause_method == "hypothesis_validation"


def test_unrooted_or_unvalidated_chain_is_not_identified():
    """A chain emitted but whose root carries no causal evidence stays a
    CANDIDATE — IDENTIFIED is earned by grounding the root, not by emitting it."""
    root = _root()  # no supporting evidence
    case = _case(nodes=[root])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=d.node_id)
    ]
    hyp = _hyp(root.node_id)
    case.hypotheses = {hyp.hypothesis_id: hyp}
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state != CauseState.IDENTIFIED


def test_refuted_hypothesis_root_does_not_ground():
    case, root, hyp = _chain_case()
    root.node_state = NodeState.VALIDATED
    root.validation_method = ValidationMethod.EMPIRICAL
    hyp.state = HypothesisState.REFUTED
    assert any_chain_root_validated(case) is False


# ---------------------------------------------------------------------------
# M6 / Option (c) — durable, evidence-driven demotion
# ---------------------------------------------------------------------------


def test_attach_engine_refutation_creates_durable_absence_evidence():
    root = _root()
    case = _case(nodes=[root])
    _attach_engine_refutation(case, root.node_id, "failed treatment")
    assert len(case.evidence) == 1
    ev = case.evidence[0]
    assert ev.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
    assert root.evidence_links[-1].stance == EvidenceStance.REFUTES
    assert root.evidence_links[-1].evidence_id == ev.evidence_id
    # idempotent — a second call adds nothing (node already has a refuting link)
    _attach_engine_refutation(case, root.node_id, "again")
    assert len(case.evidence) == 1


def test_demote_via_evidence_refutes_and_retracts():
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)  # grounds it → IDENTIFIED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    # failed treatment: the hypothesis is net-refuted
    hyp.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=hyp.hypothesis_id,
            evidence_id=_eid("fail"),
            stance=EvidenceStance.REFUTES,
            reasoning="fix applied, D persists",
            stance_confidence=0.9,
        )
    )
    acted = demote_disconfirmed_cause_via_evidence(case)
    assert acted is True
    assert case.root_cause_conclusion is None  # retracted
    assert any(
        link.stance == EvidenceStance.REFUTES for link in root.evidence_links
    )  # durable refutation attached


def test_turn28_no_resurrection_under_chain_mode():
    """The core Option-(c) guarantee: after a failed-treatment demotion, the root
    stays REFUTED on the NEXT recompute — it is not re-validated from the stale
    supporting evidence. (The turn-28 bug was exactly this resurrection.)"""
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED

    # Treatment fails: net-refute the hypothesis, then recompute.
    hyp.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=hyp.hypothesis_id,
            evidence_id=_eid("fail"),
            stance=EvidenceStance.REFUTES,
            reasoning="fix applied, D persists",
            stance_confidence=0.9,
        )
    )
    _recompute_cause_state_from_chain(case)
    assert root.node_state == NodeState.REFUTED
    assert case.progress.cause_state != CauseState.IDENTIFIED

    # NEXT turn: recompute again — the durable refutation keeps the root down.
    _recompute_cause_state_from_chain(case)
    assert root.node_state == NodeState.REFUTED
    assert case.progress.cause_state != CauseState.IDENTIFIED


# ---------------------------------------------------------------------------
# Flag gating — chain path only when the flag is on
# ---------------------------------------------------------------------------


def test_recompute_uses_chain_path_when_flag_on(monkeypatch):
    case, root, hyp = _chain_case()
    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: SimpleNamespace(
            features=SimpleNamespace(enable_hypothesis_chain_emission=True)
        ),
    )
    _recompute_assessment_state(case)
    # chain path validated the root from its evidence
    assert root.node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED


def test_recompute_uses_flat_path_when_flag_off(monkeypatch):
    """Flag OFF: the chain root is NOT consulted for cause_state (flat grounding
    governs), so an evidence-backed root does not by itself flip IDENTIFIED."""
    case, root, hyp = _chain_case()
    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: SimpleNamespace(
            features=SimpleNamespace(enable_hypothesis_chain_emission=False)
        ),
    )
    _recompute_assessment_state(case)
    # flat path: no RootCauseConclusion / grounding → not IDENTIFIED, and derive
    # did not run, so the root is untouched.
    assert root.node_state == NodeState.CANDIDATE
    assert case.progress.cause_state != CauseState.IDENTIFIED


# ---------------------------------------------------------------------------
# Code-review fixes
# ---------------------------------------------------------------------------


def test_retired_hypothesis_root_does_not_ground():
    """A RETIRED (abandoned) hypothesis whose root is still VALIDATED must NOT
    keep grounding IDENTIFIED — only ACTIVE/VALIDATED hypotheses are standing."""
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    hyp.state = HypothesisState.RETIRED
    assert any_chain_root_validated(case) is False
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state != CauseState.IDENTIFIED


def test_node_only_counterfactual_refute_retracts_conclusion():
    """The disconfirmation lands on the ROOT NODE (not the flat hypothesis), as
    the prompt mandates. M6 must still fire: cause_state drops AND the stale
    conclusion is retracted (no truth-split with the disposition layer)."""
    case, root, hyp = _chain_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="pool exhausted",
        mechanism="leak",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
    )
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    # Counterfactual REFUTES on the ROOT NODE only — the hypothesis is untouched.
    absent = _evidence("ev_absent", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(absent)
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absent.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="fix applied, D persists",
            linked_at_turn=case.current_turn,
        )
    )
    _recompute_cause_state_from_chain(case)
    assert root.node_state == NodeState.REFUTED
    assert case.progress.cause_state != CauseState.IDENTIFIED
    assert case.root_cause_conclusion is None  # retracted via node-side trigger


def test_ordinary_refute_does_not_suppress_decisive_attach():
    """A pre-existing ordinary (non-counterfactual) refute must NOT block M6 from
    attaching its DECISIVE CAUSAL_ABSENCE refutation."""
    from faultmaven.core.investigation.causal_graph import _attach_engine_refutation

    ordinary = _evidence("ev_ordinary", EvidenceCategory.CAUSAL_EVIDENCE)
    root = _root(support_label="ev_root_support")
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=ordinary.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="weak contra",
            linked_at_turn=2,
        )
    )
    case = _case(nodes=[root], evidence=[_evidence("ev_root_support"), ordinary])
    _attach_engine_refutation(case, root.node_id, "failed treatment")
    # a CAUSAL_ABSENCE row was added despite the pre-existing ordinary refute
    assert any(
        e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE for e in case.evidence
    )


def test_one_chain_demoted_other_standing_stays_identified():
    """Two chains; one is counterfactually disconfirmed, the other's root stays
    validated → the case remains IDENTIFIED via the standing chain."""
    r1 = _root("cn_000000000001", support_label="ev_s1")
    r2 = _root("cn_000000000002", support_label="ev_s2")
    case = _case(nodes=[r1, r2], evidence=[_evidence("ev_s1"), _evidence("ev_s2")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=r1.node_id, effect_node_id=d.node_id),
        CausalEdge(cause_node_id=r2.node_id, effect_node_id=d.node_id),
    ]
    h1 = _hyp(r1.node_id)
    h1.hypothesis_id = "hyp_000000000001"
    h2 = _hyp(r2.node_id)
    h2.hypothesis_id = "hyp_000000000002"
    case.hypotheses = {h1.hypothesis_id: h1, h2.hypothesis_id: h2}
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    # Disconfirm chain 1 only (counterfactual on its root).
    absent = _evidence("ev_absent1", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(absent)
    r1.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absent.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="fix on cause 1 failed",
            linked_at_turn=case.current_turn,
        )
    )
    _recompute_cause_state_from_chain(case)
    assert r1.node_state == NodeState.REFUTED
    assert r2.node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED  # standing via chain 2
