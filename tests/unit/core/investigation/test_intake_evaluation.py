"""Process-owned intake-evaluation orchestration (``run_intake_evaluation``).

The matcher-owned pieces (the evaluator + the root resolver) are injected here so
this exercises only the process orchestration: verdict → node-evidence link with
provenance, lazy promotion on a SUPPORTS, no instantiation on a REFUTES, dedup,
and skipping verdicts for unknown candidates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.differential_intake import StanceVerdict
from faultmaven.core.investigation.intake_evaluation import run_intake_evaluation
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit

ROOT_ID = "cn_000000000001"


def _evidence(eid: str = "ev_000000000001") -> Evidence:
    return Evidence(
        evidence_id=eid,
        summary="an observed fact",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(UTC),
    )


def _root(node_id: str = ROOT_ID) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement="the root cause",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        generated_at_turn=1,
    )


def _case(node: CausalNode | None = None) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="x",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="x", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = 5
    if node is not None:
        case.causal_nodes = {node.node_id: node}
    return case


def _ac(candidate_id: str = "rb1:A"):
    # Stand-in for the matcher's ActiveCause (candidate_id + record). The record
    # is opaque here — only the injected resolver consumes it.
    return SimpleNamespace(candidate_id=candidate_id, record=object())


def _verdict(
    *,
    cause_id: str = "rb1:A",
    stance: EvidenceStance = EvidenceStance.SUPPORTS,
    provenance: str = "runbook",
) -> StanceVerdict:
    return StanceVerdict(
        cause_id=cause_id,
        stance=stance,
        provenance=provenance,  # type: ignore[arg-type]
        predicate={"predicate": "contains", "target": "NotFound"},
    )


def _always(root_id):
    """Resolver that always returns root_id (cause already instantiated)."""
    return lambda case, record, *, may_instantiate: root_id


def _promote_only(root_id):
    """Resolver that returns root_id only when allowed to instantiate (the lazy
    promotion path); None otherwise (un-promoted cause)."""
    return lambda case, record, *, may_instantiate: root_id if may_instantiate else None


def test_supports_verdict_promotes_and_links_with_provenance():
    node = _root()
    case = _case(node)
    ev = _evidence()
    recorded = run_intake_evaluation(
        case,
        [ev],
        [_ac()],
        case.current_turn,
        resolve_root=_promote_only(node.node_id),
        evaluate=lambda **_: [_verdict(provenance="runbook")],
    )
    assert len(node.evidence_links) == 1
    link = node.evidence_links[0]
    assert link.evidence_id == ev.evidence_id
    assert link.stance == EvidenceStance.SUPPORTS
    assert link.provenance == "runbook"  # carried from the verdict
    assert recorded and recorded[0].cause_id == "rb1:A"


def test_refutes_on_unpromoted_cause_is_skipped():
    node = _root()
    case = _case(node)
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_promote_only(node.node_id),  # returns None when not instantiating
        evaluate=lambda **_: [_verdict(stance=EvidenceStance.REFUTES)],
    )
    assert node.evidence_links == []
    assert recorded == []


def test_refutes_on_already_instantiated_cause_links():
    node = _root()
    case = _case(node)
    run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),  # cause already in the graph
        evaluate=lambda **_: [_verdict(stance=EvidenceStance.REFUTES)],
    )
    assert len(node.evidence_links) == 1
    assert node.evidence_links[0].stance == EvidenceStance.REFUTES


def test_duplicate_verdict_links_once():
    node = _root()
    case = _case(node)
    v = _verdict()
    run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [v, v],  # same datum fires the same predicate twice
    )
    assert len(node.evidence_links) == 1


def test_verdict_for_unknown_candidate_is_skipped():
    node = _root()
    case = _case(node)
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        [_ac(candidate_id="rb1:A")],
        case.current_turn,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict(cause_id="rb9:Z")],  # not in active_causes
    )
    assert node.evidence_links == []
    assert recorded == []


def test_fallback_provenance_is_carried_to_the_link():
    node = _root()
    case = _case(node)
    run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict(provenance="llm_fallback")],
    )
    assert node.evidence_links[0].provenance == "llm_fallback"


def test_default_evaluator_stub_is_inert():
    # With the real (stubbed) evaluator, no verdicts → no links (loop inert until
    # the matcher body lands).
    node = _root()
    case = _case(node)
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),
    )
    assert recorded == []
    assert node.evidence_links == []
