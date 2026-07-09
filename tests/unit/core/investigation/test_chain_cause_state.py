"""Gate 1b slice 2: chain-derived cause_state (Option A, §9.2) + M6 via Option (c).

``cause_state=IDENTIFIED`` is derived from a VALIDATED chain root (real rung
evidence), not flat assertion, and failed-treatment demotion is made durable by
attaching an engine REFUTES fact to the root — so ``derive_node_states`` keeps it
refuted across turns (no turn-28 resurrection).
"""

import hashlib
from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation import milestone_engine
from faultmaven.core.investigation.causal_graph import (
    _attach_engine_refutation,
    any_chain_root_validated,
    demote_disconfirmed_cause_via_evidence,
    seed_problem_node,
    synthesize_rcc_from_validated_root,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
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
    root_node_id,
    *,
    hypothesis_id="hyp_000000000001",
    refutes=0,
    supports=0,
    state=HypothesisState.ACTIVE,
) -> Hypothesis:
    links = [
        HypothesisEvidenceLink(
            hypothesis_id=hypothesis_id,
            evidence_id=_eid(f"{hypothesis_id}r{i}"),
            stance=EvidenceStance.REFUTES,
            reasoning="contra",
            stance_confidence=0.9,
        )
        for i in range(refutes)
    ] + [
        HypothesisEvidenceLink(
            hypothesis_id=hypothesis_id,
            evidence_id=_eid(f"{hypothesis_id}s{i}"),
            stance=EvidenceStance.SUPPORTS,
            reasoning="pro",
            stance_confidence=0.9,
        )
        for i in range(supports)
    ]
    return Hypothesis(
        hypothesis_id=hypothesis_id,
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
    # Cause identification is anchored on a verified symptom. These chain fixtures
    # model an in-progress investigation that has already verified its symptom (the
    # realistic state once a causal chain root validates), so the symptom-anchor
    # precondition is satisfied and the tests exercise chain mechanics, not the
    # anchor. The anchor itself is covered by the symptom-anchor tests below.
    case.progress.symptom_verified = True
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
# IDENTIFIED is anchored on a verified symptom
# ---------------------------------------------------------------------------


def test_validated_root_without_verified_symptom_holds_candidates():
    """A validated chain root with ``symptom_verified=False`` must NOT reach
    IDENTIFIED — the verified symptom is the cause-identification anchor. It holds
    at CANDIDATES (never flaps to UNKNOWN), and no RootCauseConclusion is
    synthesized while unanchored."""
    case, root, hyp = _chain_case()
    case.progress.symptom_verified = False  # anchor not yet established
    _recompute_cause_state_from_chain(case)
    # the chain still validates structurally...
    assert any_chain_root_validated(case) is True
    assert root.node_state == NodeState.VALIDATED
    # ...but cause_state is held at CANDIDATES, not promoted to IDENTIFIED
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert case.progress.cause_state != CauseState.UNKNOWN  # explicit: no collapse
    # no premature conclusion synthesized while unanchored
    assert case.root_cause_conclusion is None


def test_symptom_verification_promotes_candidates_to_identified():
    """Once the symptom verifies, the same validated-root case advances to
    IDENTIFIED — the gate is exactly the anchor, nothing else."""
    case, root, hyp = _chain_case()
    case.progress.symptom_verified = False
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.CANDIDATES

    # The user/LLM establishes the verified symptom on a later turn.
    case.progress.symptom_verified = True
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.root_cause_conclusion is not None  # now synthesized


def test_unanchored_single_hypothesis_validated_root_holds_candidates():
    """Edge: a single ACTIVE hypothesis (count < 2) whose root is validated but
    whose symptom is unverified must still hold at CANDIDATES via the validated-root
    arm — without that arm it would wrongly fall through to UNKNOWN."""
    case, root, hyp = _chain_case()  # exactly one hypothesis
    case.progress.symptom_verified = False
    assert HypothesisManager.count_active_hypotheses(case) < 2
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.CANDIDATES


# ---------------------------------------------------------------------------
# Pre-anchor hypotheses are QUEUED as CAPTURED, inert in the differential, and
# auto-promoted to ACTIVE on symptom verification
# ---------------------------------------------------------------------------


def test_captured_queued_hypothesis_is_inert_then_promotes_to_ground():
    """A hypothesis QUEUED before the anchor (CAPTURED) is inert: its validated
    root does NOT ground IDENTIFIED and it is not counted as an active candidate.
    Promoting it (auto-apply on symptom verification) makes it ACTIVE, and the
    same validated root then grounds IDENTIFIED — the full queue→flush→ground
    lifecycle, using the real engine functions."""
    case, root, hyp = _chain_case()  # validated root, symptom_verified=True
    # Queue state: the hypothesis was formed pre-anchor → CAPTURED.
    hyp.state = HypothesisState.CAPTURED

    # Inert while queued: not standing, so the validated root does not ground,
    # and it is not an active candidate.
    assert any_chain_root_validated(case) is False
    assert HypothesisManager.count_active_hypotheses(case) == 0
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state != CauseState.IDENTIFIED

    # Auto-apply on verification: promote CAPTURED → ACTIVE.
    promoted = HypothesisManager.activate_queued_hypotheses(case)
    assert promoted == [hyp.hypothesis_id]
    assert hyp.state == HypothesisState.ACTIVE

    # Now standing → the same validated root grounds IDENTIFIED (the symptom is
    # already verified in the fixture, so the anchor is satisfied).
    assert any_chain_root_validated(case) is True
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED


def test_activate_queued_promotes_only_captured():
    """The promotion helper touches ONLY CAPTURED hypotheses — ACTIVE/REFUTED/
    RETIRED are left untouched (forward-only flush of the queue)."""
    captured = _hyp(
        None, hypothesis_id="hyp_0000000000ca", state=HypothesisState.CAPTURED
    )
    active = _hyp(None, hypothesis_id="hyp_0000000000ac", state=HypothesisState.ACTIVE)
    retired = _hyp(
        None, hypothesis_id="hyp_0000000000ed", state=HypothesisState.RETIRED
    )
    case = _case(hyps=[captured, active, retired])

    promoted = HypothesisManager.activate_queued_hypotheses(case)

    assert promoted == [captured.hypothesis_id]
    assert captured.state == HypothesisState.ACTIVE
    assert active.state == HypothesisState.ACTIVE  # untouched
    assert retired.state == HypothesisState.RETIRED  # untouched
    # idempotent: a second flush promotes nothing (queue drained)
    assert HypothesisManager.activate_queued_hypotheses(case) == []


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


def test_turn28_no_resurrection_chain_derived():
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


def test_recompute_uses_chain_path():
    case, root, hyp = _chain_case()
    _recompute_assessment_state(case)
    # chain path validated the root from its evidence
    assert root.node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED


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


def test_demote_via_evidence_noop_when_not_grounded():
    """M6 must not fire on an ungrounded case (cause_state != IDENTIFIED) — no
    refutation, no fabricated CAUSAL_ABSENCE row, no retraction."""
    case, root, hyp = _chain_case()  # cause_state defaults to UNKNOWN
    assert case.progress.cause_state != CauseState.IDENTIFIED
    before = len(case.evidence)
    assert demote_disconfirmed_cause_via_evidence(case) is False
    assert len(case.evidence) == before  # no engine refutation minted


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
    h1 = _hyp(r1.node_id, hypothesis_id="hyp_000000000001")
    h2 = _hyp(r2.node_id, hypothesis_id="hyp_000000000002")
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
    # §9.3: the conclusion mirrors the SURVIVING chain (not left None by chain 1's
    # demotion) — no IDENTIFIED-with-empty-RCC truth split.
    assert case.root_cause_conclusion is not None
    assert case.root_cause_conclusion.validated_hypothesis_id == h2.hypothesis_id


# ---------------------------------------------------------------------------
# finding-5: soft floor (INCONCLUSIVE root holds CANDIDATES, REFUTED drops fully)
# ---------------------------------------------------------------------------


def test_soft_floor_inconclusive_root_holds_candidates_not_unknown():
    """A once-IDENTIFIED root that loses validation to an evidence TIE goes
    INCONCLUSIVE (not REFUTED); cause_state holds at CANDIDATES rather than
    flapping to UNKNOWN (finding-5 / NO-COLLAPSE) — there is only one hypothesis,
    so without the soft floor it would drop to UNKNOWN."""
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED

    # Ordinary (non-counterfactual) causal REFUTES ties the root's support →
    # derive marks it INCONCLUSIVE (not REFUTED), and the hyp itself is untouched.
    tie = _evidence("ev_tie", EvidenceCategory.CAUSAL_EVIDENCE)
    case.evidence.append(tie)
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=tie.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="one contrary reading",
            linked_at_turn=case.current_turn,
        )
    )
    _recompute_cause_state_from_chain(case)
    assert root.node_state == NodeState.INCONCLUSIVE
    assert case.progress.cause_state == CauseState.CANDIDATES  # soft floor, not UNKNOWN


def test_counterfactual_refute_drops_fully_not_floored():
    """A counterfactual (CAUSAL_ABSENCE) refute REFUTES the root decisively — that
    is a real disconfirmation (M6), so the case drops FULLY (UNKNOWN), not held at
    CANDIDATES by the soft floor."""
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED

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
    assert case.progress.cause_state == CauseState.UNKNOWN  # full drop, not floored


# ---------------------------------------------------------------------------
# §9.3: synthesize a RootCauseConclusion from the validated chain root
# ---------------------------------------------------------------------------


def test_synthesize_rcc_when_validated_root_has_no_conclusion():
    """Chain-validated cause with no LLM conclusion: the engine mirrors the
    validated root into an RCC so the disposition/report layer has cause text."""
    case, root, hyp = _chain_case()
    assert case.root_cause_conclusion is None
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.root_cause == root.statement
    assert rcc.validated_hypothesis_id == hyp.hypothesis_id
    # M2: empirical validation is mechanistic grade — CONFIDENT, never VERIFIED
    # (0.9/"verified" is reserved for counterfactual confirmation).
    assert rcc.confidence_level == ConfidenceLevel.CONFIDENT
    assert rcc.likelihood == 0.8


def test_synthesized_rcc_does_not_overwrite_llm_conclusion():
    case, root, hyp = _chain_case()
    own = RootCauseConclusion(
        root_cause="the LLM's own worded conclusion",
        mechanism="as the LLM described it",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
    )
    case.root_cause_conclusion = own
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.root_cause_conclusion is own  # LLM's conclusion preserved


def test_stale_engine_rcc_refreshed_on_root_handoff_without_refutation():
    """The non-M6 handoff: chain A grounds (engine RCC for A); next turn A drifts
    to INCONCLUSIVE (a tie, NOT refuted, so M6 never clears the RCC) while chain B
    validates. The stale engine RCC must REFRESH to B, not keep naming A."""
    rA = _root("cn_00000000000a", support_label="ev_sa")
    rB = _root("cn_00000000000b")  # B not yet supported
    case = _case(nodes=[rA, rB], evidence=[_evidence("ev_sa")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=rA.node_id, effect_node_id=d.node_id),
        CausalEdge(cause_node_id=rB.node_id, effect_node_id=d.node_id),
    ]
    hA = _hyp(rA.node_id, hypothesis_id="hyp_0000000000aa")
    hB = _hyp(rB.node_id, hypothesis_id="hyp_0000000000bb")
    case.hypotheses = {hA.hypothesis_id: hA, hB.hypothesis_id: hB}
    _recompute_cause_state_from_chain(case)  # A validated → engine RCC names hA
    assert case.root_cause_conclusion.validated_hypothesis_id == hA.hypothesis_id

    # A drifts to a tie (INCONCLUSIVE, not refuted); B gains causal support.
    tie = _evidence("ev_tieA", EvidenceCategory.CAUSAL_EVIDENCE)
    sb = _evidence("ev_sb", EvidenceCategory.CAUSAL_EVIDENCE)
    case.evidence += [tie, sb]
    rA.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=tie.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="contra",
            linked_at_turn=case.current_turn,
        )
    )
    rB.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=sb.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning="pro",
            linked_at_turn=case.current_turn,
        )
    )
    _recompute_cause_state_from_chain(case)
    assert rA.node_state == NodeState.INCONCLUSIVE  # drifted, not refuted
    assert rB.node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED  # via B
    # the stale engine mirror refreshed to the surviving grounding root
    assert case.root_cause_conclusion.validated_hypothesis_id == hB.hypothesis_id


def test_deductive_root_rcc_is_confident_grade():
    """A deductively-validated root yields a CONFIDENT (mechanistic-grade) RCC,
    not VERIFIED (M2: deduction is validation, not counterfactual
    confirmation), and the no-intermediate mechanism fallback."""
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)  # validates empirically first
    # Flip the (now validated) root to deductive grade, clear the RCC to force a
    # fresh synthesis, and re-run.
    root.validation_method = ValidationMethod.DEDUCTIVE
    case.root_cause_conclusion = None
    _recompute_cause_state_from_chain(case)
    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.confidence_level == ConfidenceLevel.CONFIDENT  # deductive ≠ VERIFIED
    assert (
        rcc.mechanism == "Directly produces the observed problem."
    )  # degenerate chain


def _confirm(case, root, label="ev_absence"):
    """Attach a counterfactual confirmation (causal_absence SUPPORTS) to the root."""
    absent = _evidence(label, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(absent)
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absent.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning="removing the cause removed the problem",
            linked_at_turn=case.current_turn,
        )
    )


def test_confirmed_root_rcc_is_verified():
    """M2 gone⇒gone: only a counterfactually confirmed root mints a VERIFIED
    (≥0.9) engine conclusion."""
    case, root, hyp = _chain_case()
    _confirm(case, root)
    _recompute_cause_state_from_chain(case)
    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.confidence_level == ConfidenceLevel.VERIFIED
    assert rcc.likelihood >= 0.9


def test_mechanistic_rcc_caps_llm_likelihood():
    """The cap is a CAP: the LLM's own higher root_cause_likelihood must not
    push an unconfirmed (mechanistic) engine mirror into 'verified' — the #656
    turn-6 shape."""
    case, root, hyp = _chain_case()
    case.progress.root_cause_likelihood = 0.95  # LLM-asserted near-certainty
    _recompute_cause_state_from_chain(case)
    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.confidence_level == ConfidenceLevel.CONFIDENT
    assert rcc.likelihood == 0.8


def test_engine_mirror_upgrades_when_confirmation_arrives():
    """A standing mechanistic engine mirror re-mints to VERIFIED the turn its
    root gains the counterfactual confirmation."""
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion.confidence_level == ConfidenceLevel.CONFIDENT
    _confirm(case, root)
    _recompute_cause_state_from_chain(case)
    rcc = case.root_cause_conclusion
    assert rcc.confidence_level == ConfidenceLevel.VERIFIED
    assert rcc.validated_hypothesis_id == hyp.hypothesis_id


def test_pre_cap_verified_engine_mirror_corrects_down():
    """A persisted engine mirror that predates the M2 cap (VERIFIED/0.9 on a
    root that was never confirmed) self-corrects to CONFIDENT on the next
    recompute — the grade, not the stale claim, rules the mirror."""
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)  # root VALIDATED, mirror CONFIDENT
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=root.statement,
        mechanism="Directly produces the observed problem.",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
        validated_hypothesis_id=hyp.hypothesis_id,
        determined_by="engine:chain_validation",
    )
    _recompute_cause_state_from_chain(case)
    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.confidence_level == ConfidenceLevel.CONFIDENT
    assert rcc.likelihood == 0.8
    assert rcc.validated_hypothesis_id == hyp.hypothesis_id


def test_llm_authored_verified_rcc_is_not_touched_by_the_cap():
    """The M2 cap governs the ENGINE's mirror only: an LLM-authored conclusion
    is never rewritten here (its retraction lifecycle is #656 P2.3) — the
    over-claim is surfaced via the persisted grade + the seam warning instead."""
    case, root, hyp = _chain_case()
    own = RootCauseConclusion(
        root_cause="the LLM's own worded conclusion",
        mechanism="as the LLM described it",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.95,
    )
    case.root_cause_conclusion = own
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion is own


# Source-of-truth RCC retraction (soundness): a RootCauseConclusion whose NAMED
# cause (validated_hypothesis_id) is disconfirmed is cleared at the source, so NO
# consumer (terminal gate, report, UI, KB runbook) asserts a disproven cause.
# Covers the gap M6 misses when cause_state never reached IDENTIFIED. Link-based
# ONLY (no likelihood proxy) — so a valid RCC is never wrongly cleared.
def _rcc(*, vhid=None):
    return RootCauseConclusion(
        root_cause="connection pool exhausted",
        mechanism="leak",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
        validated_hypothesis_id=vhid,
    )


def test_retract_disconfirmed_rcc_clears_when_named_hyp_refuted():
    from faultmaven.core.investigation.causal_graph import retract_disconfirmed_rcc

    hyp = _hyp(root_node_id=None, hypothesis_id="hyp_0000000000ab")
    # REFUTED set post-construction (the constructor validator requires a
    # refutation_reason pairing; assignment does not re-validate).
    hyp.state = HypothesisState.REFUTED
    hyp.refutation_reason = "fix applied, problem persisted"
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _rcc(vhid=hyp.hypothesis_id)
    assert retract_disconfirmed_rcc(case) is True
    assert case.root_cause_conclusion is None


def test_retract_disconfirmed_rcc_keeps_when_named_hyp_active():
    from faultmaven.core.investigation.causal_graph import retract_disconfirmed_rcc

    hyp = _hyp(
        root_node_id=None,
        hypothesis_id="hyp_0000000000ac",
        state=HypothesisState.ACTIVE,
    )
    case = _case(hyps=[hyp])
    rcc = _rcc(vhid=hyp.hypothesis_id)
    case.root_cause_conclusion = rcc
    assert retract_disconfirmed_rcc(case) is False
    assert case.root_cause_conclusion is rcc  # untouched


def test_retract_disconfirmed_rcc_ignores_unlinked_rcc():
    # Link-based ONLY: a free-text RCC with no validated_hypothesis_id is left
    # untouched even amid a refuted hypothesis — no likelihood proxy, so no
    # false-clear of a possibly-valid conclusion (the regression the proxy caused;
    # documented residual instead).
    from faultmaven.core.investigation.causal_graph import retract_disconfirmed_rcc

    refuted = _hyp(root_node_id=None, hypothesis_id="hyp_0000000000ad")
    refuted.state = HypothesisState.REFUTED
    refuted.refutation_reason = "ruled out"
    case = _case(hyps=[refuted])
    rcc = _rcc(vhid=None)
    case.root_cause_conclusion = rcc
    assert retract_disconfirmed_rcc(case) is False
    assert case.root_cause_conclusion is rcc


def test_synthesize_mirrors_any_standing_validated_root():
    """The mirror follows the validation verdict — the §7.1 guard is an ENTRY
    bar on validation, not a mint-time re-adjudication. A root that stands
    VALIDATED (here: deductively; the same holds for the grandfathered
    pre-guard class) is mirrorable even when its statement restates the frame;
    refusing it would split cause_state=IDENTIFIED from a permanently-absent
    conclusion."""
    case, root, hyp = _chain_case()
    object.__setattr__(root, "statement", "orders failing")
    object.__setattr__(root, "node_state", NodeState.VALIDATED)
    object.__setattr__(root, "validation_method", ValidationMethod.DEDUCTIVE)
    assert synthesize_rcc_from_validated_root(case) is True
    assert case.root_cause_conclusion is not None


def test_synthesize_rcc_mints_from_a_mechanism_root():
    """The common path: a deductively validated mechanism root mints."""
    case, root, hyp = _chain_case()
    object.__setattr__(
        root, "statement", "checkout service DB pool sized below peak demand"
    )
    object.__setattr__(root, "node_state", NodeState.VALIDATED)
    object.__setattr__(root, "validation_method", ValidationMethod.DEDUCTIVE)
    assert synthesize_rcc_from_validated_root(case) is True
    assert case.root_cause_conclusion is not None
    assert case.root_cause_conclusion.root_cause == root.statement


def test_stale_engine_rcc_cleared_when_root_demotes_and_nothing_validates():
    """Engine-mirror coherence on the (evidence-driven) demotion path: an
    ENGINE-authored RCC whose grounding root demotes out of VALIDATED (its
    backing support evaporates) is CLEARED by the recompute — the mirror must
    not outlive its chain, or readiness/report readers (which key on RCC
    presence) keep asserting a conclusion nothing grounds."""
    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    engine_rcc = case.root_cause_conclusion
    assert engine_rcc is not None  # engine mirror minted

    # Support evaporates: the backing evidence row is gone, so the next derive
    # demotes the root (dangling links never count).
    case.evidence = []
    _recompute_cause_state_from_chain(case)

    assert root.node_state != NodeState.VALIDATED
    assert case.root_cause_conclusion is None  # mirror cleared with its chain


def test_llm_rcc_survives_root_demotion():
    """The engine-mirror reconcile never touches an LLM-authored conclusion —
    its retraction lifecycle is a separate concern (#656)."""
    case, root, hyp = _chain_case()
    own = RootCauseConclusion(
        root_cause="the LLM's own worded conclusion",
        mechanism="as the LLM described it",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
    )
    case.root_cause_conclusion = own
    _recompute_cause_state_from_chain(case)
    case.evidence = []
    _recompute_cause_state_from_chain(case)
    assert root.node_state != NodeState.VALIDATED
    assert case.root_cause_conclusion is own  # untouched


# ---------------------------------------------------------------------------
# M2 assurance grade: per-turn persistence + the over-claim seam warning
# ---------------------------------------------------------------------------


def test_recompute_persists_cause_assurance_grade():
    """#656 DF-6: the grade is written onto the progress blob each recompute so
    the grade × conclusion-confidence seam is queryable, and it tracks the
    graph as confirmation arrives."""
    from faultmaven.modules.case.contracts import CauseAssuranceGrade

    case, root, hyp = _chain_case()
    assert case.progress.cause_assurance == CauseAssuranceGrade.NO_ROOT  # default
    _recompute_assessment_state(case)
    assert case.progress.cause_assurance == CauseAssuranceGrade.MECHANISTIC
    absent = _evidence("ev_conf", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(absent)
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absent.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning="removing the cause removed the problem",
            linked_at_turn=case.current_turn,
        )
    )
    _recompute_assessment_state(case)
    assert case.progress.cause_assurance == CauseAssuranceGrade.CONFIRMED


def test_cause_assurance_rides_the_progress_blob():
    """Persistence pin: the grade serializes with InvestigationProgress (the
    verification_status pattern — no migration), and a pre-P1.2 blob without
    the field reloads at the NO_ROOT default."""
    import json

    from faultmaven.modules.case.contracts import CauseAssuranceGrade
    from faultmaven.modules.case.domain.models import InvestigationProgress

    p = InvestigationProgress(cause_assurance=CauseAssuranceGrade.CONFIRMED)
    blob = json.loads(p.model_dump_json())
    assert blob["cause_assurance"] == "confirmed"
    assert (
        InvestigationProgress(**blob).cause_assurance == CauseAssuranceGrade.CONFIRMED
    )
    blob.pop("cause_assurance")  # a blob persisted before the field existed
    assert InvestigationProgress(**blob).cause_assurance == CauseAssuranceGrade.NO_ROOT


def test_overclaim_warning_fires_for_llm_verified_rcc(caplog):
    """The prod-visible over-claim seam (#656 turn-6 shape): an LLM-authored
    conclusion claiming verified while the grade lacks counterfactual
    confirmation warns every recompute."""
    import logging as _logging

    case, root, hyp = _chain_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the LLM's own worded conclusion",
        mechanism="as the LLM described it",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.95,
    )
    with caplog.at_level(
        _logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
    ):
        _recompute_assessment_state(case)
    recs = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "cause_confidence_overclaim"
    ]
    assert recs, "over-claim seam warning not emitted"
    assert recs[-1].cause_assurance == "mechanistic"


def test_no_overclaim_warning_for_grade_consistent_mirror(caplog):
    """Control: the engine's own capped mirror (CONFIDENT on mechanistic) never
    trips the over-claim warning."""
    import logging as _logging

    case, root, hyp = _chain_case()
    case.progress.symptom_verified = True
    with caplog.at_level(
        _logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
    ):
        _recompute_assessment_state(case)
    assert not [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "cause_confidence_overclaim"
    ]


# ---------------------------------------------------------------------------
# M2 confirm-side stamp: the engine makes CONFIRMED reachable from the live flow
# (the prompt records the resolution causal_absence row UNLINKED by contract)
# ---------------------------------------------------------------------------


def _standalone_absence(case, label="ev_standalone_absence"):
    """The prompt-contract shape: a causal_absence row on the case, linked to
    NOTHING."""
    row = _evidence(label, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(row)
    return row


def test_stamp_confirms_sole_validated_root_from_standalone_absence():
    """Happy path: fix confirmed → standalone causal_absence row recorded →
    the engine attaches the SUPPORTS link, and the same recompute reads
    CONFIRMED with a VERIFIED mirror."""
    from faultmaven.modules.case.contracts import CauseAssuranceGrade

    case, root, hyp = _chain_case()
    _recompute_assessment_state(case)  # root validates (mechanistic)
    assert case.progress.cause_assurance == CauseAssuranceGrade.MECHANISTIC
    _standalone_absence(case)
    _recompute_assessment_state(case)
    assert case.progress.cause_assurance == CauseAssuranceGrade.CONFIRMED
    assert case.root_cause_conclusion.confidence_level == ConfidenceLevel.VERIFIED
    # Idempotent: another recompute attaches nothing further.
    links_before = len(root.evidence_links)
    _recompute_assessment_state(case)
    assert len(root.evidence_links) == links_before


def test_stamp_never_confirms_from_a_refutes_linked_absence_row():
    """A REFUTES-linked absence row is a failed-fix disconfirmation (M6) —
    it must never flip into a confirmation of another root."""
    from faultmaven.core.investigation.causal_graph import (
        confirm_root_from_resolution_absence,
    )

    case, root, hyp = _chain_case()
    _recompute_cause_state_from_chain(case)
    absent = _evidence("ev_failed_fix", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(absent)
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absent.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="fix applied, D persists",
            linked_at_turn=case.current_turn,
        )
    )
    assert confirm_root_from_resolution_absence(case) is False


def test_stamp_holds_when_multiple_roots_stand_validated():
    """MECE-violation guard: with two standing validated roots the engine never
    guesses which cause the fix removed — no link, grade stays MECHANISTIC."""
    from faultmaven.core.investigation.causal_graph import (
        confirm_root_from_resolution_absence,
    )
    from faultmaven.modules.case.contracts import CauseAssuranceGrade, NodeState

    rA = _root("cn_00000000000a", support_label="ev_sa")
    rB = _root("cn_00000000000b", support_label="ev_sb")
    case = _case(nodes=[rA, rB], evidence=[_evidence("ev_sa"), _evidence("ev_sb")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=rA.node_id, effect_node_id=d.node_id),
        CausalEdge(cause_node_id=rB.node_id, effect_node_id=d.node_id),
    ]
    hA = _hyp(rA.node_id, hypothesis_id="hyp_0000000000aa")
    hB = _hyp(rB.node_id, hypothesis_id="hyp_0000000000bb")
    case.hypotheses = {hA.hypothesis_id: hA, hB.hypothesis_id: hB}
    _recompute_cause_state_from_chain(case)
    assert rA.node_state == NodeState.VALIDATED
    assert rB.node_state == NodeState.VALIDATED
    _standalone_absence(case)
    assert confirm_root_from_resolution_absence(case) is False
    from faultmaven.core.investigation.cause_assurance import grade_cause_assurance

    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


# ---------------------------------------------------------------------------
# Mirror re-mint root selection: the conclusion follows the CONFIRMED root and
# a level-only correction never swaps the named cause
# ---------------------------------------------------------------------------


def _two_root_case_with_mirror_on_b():
    rA = _root("cn_00000000000a", support_label="ev_sa")
    rB = _root("cn_00000000000b", support_label="ev_sb")
    case = _case(nodes=[rA, rB], evidence=[_evidence("ev_sa"), _evidence("ev_sb")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=rA.node_id, effect_node_id=d.node_id),
        CausalEdge(cause_node_id=rB.node_id, effect_node_id=d.node_id),
    ]
    hA = _hyp(rA.node_id, hypothesis_id="hyp_0000000000aa")
    hB = _hyp(rB.node_id, hypothesis_id="hyp_0000000000bb")
    case.hypotheses = {hA.hypothesis_id: hA, hB.hypothesis_id: hB}
    _recompute_cause_state_from_chain(case)
    # Force the engine mirror onto B (dict order would pick A).
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=rB.statement,
        mechanism="Directly produces the observed problem.",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        validated_hypothesis_id=hB.hypothesis_id,
        determined_by="engine:chain_validation",
    )
    return case, rA, rB, hA, hB


def test_mirror_upgrade_keeps_the_confirmed_prior_root():
    """Confirmation lands on the mirror's OWN root B: the mirror upgrades to
    VERIFIED still naming B — never silently swapped to first-in-dict A."""
    case, rA, rB, hA, hB = _two_root_case_with_mirror_on_b()
    _confirm(case, rB, label="ev_confirm_b")
    _recompute_cause_state_from_chain(case)
    rcc = case.root_cause_conclusion
    assert rcc.validated_hypothesis_id == hB.hypothesis_id
    assert rcc.confidence_level == ConfidenceLevel.VERIFIED


def test_mirror_repoints_to_the_confirmed_sibling_root():
    """Confirmation lands on the OTHER root A while the mirror names B: the
    mirror re-points to the confirmed root — a conclusion asserting an
    unconfirmed sibling under a CONFIRMED grade is not faithful."""
    case, rA, rB, hA, hB = _two_root_case_with_mirror_on_b()
    _confirm(case, rA, label="ev_confirm_a")
    _recompute_cause_state_from_chain(case)
    rcc = case.root_cause_conclusion
    assert rcc.validated_hypothesis_id == hA.hypothesis_id
    assert rcc.confidence_level == ConfidenceLevel.VERIFIED


def test_level_only_correction_keeps_the_prior_root():
    """Pre-cap VERIFIED mirror on unconfirmed B (A also validated, neither
    confirmed): the correction fixes the LEVEL but keeps naming B."""
    case, rA, rB, hA, hB = _two_root_case_with_mirror_on_b()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=rB.statement,
        mechanism="Directly produces the observed problem.",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
        validated_hypothesis_id=hB.hypothesis_id,
        determined_by="engine:chain_validation",
    )
    _recompute_cause_state_from_chain(case)
    rcc = case.root_cause_conclusion
    assert rcc.validated_hypothesis_id == hB.hypothesis_id
    assert rcc.confidence_level == ConfidenceLevel.CONFIDENT
    assert rcc.likelihood == 0.8


# ---------------------------------------------------------------------------
# Over-claim WARNING is edge-triggered (warn on the transition, not per turn)
# ---------------------------------------------------------------------------


def test_overclaim_warning_is_edge_triggered(caplog):
    import logging as _logging

    case, root, hyp = _chain_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the LLM's own worded conclusion",
        mechanism="as the LLM described it",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.95,
    )
    with caplog.at_level(
        _logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
    ):
        _recompute_assessment_state(case)
        _recompute_assessment_state(case)  # standing over-claim: no second warn
    recs = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "cause_confidence_overclaim"
    ]
    assert len(recs) == 1
    assert case.progress.cause_overclaim is True
