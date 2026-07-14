"""#695 Defect A — hypothesis VALIDATED is derived from the chain root's
``node_state`` (``project_hypothesis_states_from_roots``), the SOLE producer of a
VALIDATED hypothesis. The flat likelihood-threshold transition was removed, so the
report's "Validated" bucket, the cause grade, the runbook gate, and ``cause_state``
resolve to ONE determination and can no longer disagree (the #695 divergence).

Invariant: hypothesis VALIDATED ⟺ its chain root node VALIDATED. REFUTED stays
owned by the explicit/M6 refutation lifecycle and is never projected or clobbered.
"""

import hashlib
import inspect
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.core.investigation import causal_graph as causal_graph_module
from faultmaven.core.investigation.causal_graph import (
    project_hypothesis_states_from_roots,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _nid(seed: int) -> str:
    return f"cn_{seed:012x}"


def _root(node_id: str, node_state: NodeState) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=f"root cause {node_id}",
        node_type=NodeType.ROOT,
        node_state=node_state,
        validation_method=(
            ValidationMethod.EMPIRICAL
            if node_state == NodeState.VALIDATED
            else ValidationMethod.NONE
        ),
        belief=0.9 if node_state == NodeState.VALIDATED else 0.5,
        actionable=True,
        evidence_links=[],
        generated_at_turn=1,
        refutation_reason=(
            "correlationally refuted" if node_state == NodeState.REFUTED else None
        ),
    )


def _hyp(state: HypothesisState, root_node_id: str | None) -> Hypothesis:
    return Hypothesis(
        statement="the cause",
        category=HypothesisCategory.CONFIG,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=1,
        rationale="r",
        state=state,
        root_node_id=root_node_id,
        refutation_reason=(
            "prior refutation" if state == HypothesisState.REFUTED else None
        ),
    )


def _case(*, nodes=None, hyps=None) -> Case:
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
    case.current_turn = 5
    case.causal_nodes = {n.node_id: n for n in (nodes or [])}
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    return case


# --------------------------------------------------------------------------- #
# Core projection
# --------------------------------------------------------------------------- #


def test_validated_root_projects_hypothesis_to_validated():
    root = _root(_nid(1), NodeState.VALIDATED)
    hyp = _hyp(HypothesisState.ACTIVE, root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    assert project_hypothesis_states_from_roots(case) is True
    assert hyp.state == HypothesisState.VALIDATED


@pytest.mark.parametrize(
    "node_state",
    [NodeState.CANDIDATE, NodeState.INCONCLUSIVE, NodeState.REFUTED],
)
def test_non_validated_root_leaves_hypothesis_active(node_state):
    root = _root(_nid(2), node_state)
    hyp = _hyp(HypothesisState.ACTIVE, root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    assert project_hypothesis_states_from_roots(case) is False
    assert hyp.state == HypothesisState.ACTIVE


def test_stale_validated_reverts_to_active_when_root_loses_validation():
    # A hypothesis previously projected VALIDATED whose root is now INCONCLUSIVE
    # (e.g. a fresh evidence tie, or the restatement guard on a pre-guard case).
    root = _root(_nid(3), NodeState.INCONCLUSIVE)
    hyp = _hyp(HypothesisState.VALIDATED, root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    assert project_hypothesis_states_from_roots(case) is True
    assert hyp.state == HypothesisState.ACTIVE


def test_hypothesis_without_root_never_validates():
    hyp = _hyp(HypothesisState.ACTIVE, None)
    case = _case(nodes=[], hyps=[hyp])
    assert project_hypothesis_states_from_roots(case) is False
    assert hyp.state == HypothesisState.ACTIVE


def test_hypothesis_pointing_at_missing_node_never_validates():
    hyp = _hyp(HypothesisState.ACTIVE, _nid(99))  # no such node
    case = _case(nodes=[], hyps=[hyp])
    assert project_hypothesis_states_from_roots(case) is False
    assert hyp.state == HypothesisState.ACTIVE


# --------------------------------------------------------------------------- #
# Condition 2 — REFUTED / RETIRED / CAPTURED are never clobbered
# --------------------------------------------------------------------------- #


def test_refuted_hypothesis_is_never_resurrected_to_active():
    # A root that is not validated must NOT flip a REFUTED hypothesis to ACTIVE:
    # M6 may set REFUTED in the same recompute before the projection runs, and
    # the projection must not undo it (the REFUTED no-clobber rule).
    root = _root(_nid(4), NodeState.REFUTED)
    hyp = _hyp(HypothesisState.REFUTED, root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    assert project_hypothesis_states_from_roots(case) is False
    assert hyp.state == HypothesisState.REFUTED


def test_refuted_hypothesis_not_projected_even_if_root_validated():
    # Explicit refutation wins over a validated root — the projection excludes
    # REFUTED from its targets, so it neither resurrects nor re-validates it.
    root = _root(_nid(5), NodeState.VALIDATED)
    hyp = _hyp(HypothesisState.REFUTED, root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    assert project_hypothesis_states_from_roots(case) is False
    assert hyp.state == HypothesisState.REFUTED


def test_retired_and_captured_untouched():
    root = _root(_nid(6), NodeState.VALIDATED)
    retired = _hyp(HypothesisState.RETIRED, root.node_id)
    captured = _hyp(HypothesisState.CAPTURED, root.node_id)
    case = _case(nodes=[root], hyps=[retired, captured])
    assert project_hypothesis_states_from_roots(case) is False
    assert retired.state == HypothesisState.RETIRED
    assert captured.state == HypothesisState.CAPTURED


# --------------------------------------------------------------------------- #
# Condition 4 — residual + sole-writer + refutation-path-still-carries
# --------------------------------------------------------------------------- #


def test_residual_correlationally_refuted_root_leaves_hypothesis_active():
    """Accepted residual (intentional, guarantee-safe): a root REFUTED purely
    correlationally, with its hypothesis not itself explicitly refuted, leaves the
    hypothesis ACTIVE — the projection is VALIDATED-only, it does not project
    REFUTED from node_state. Certifies nothing (NO INCORRECT CONCLUSION intact),
    does not cave (NO COLLAPSE intact)."""
    root = _root(_nid(7), NodeState.REFUTED)
    hyp = _hyp(HypothesisState.ACTIVE, root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    project_hypothesis_states_from_roots(case)
    assert hyp.state == HypothesisState.ACTIVE


def test_projection_is_the_sole_source_writer_of_validated():
    """Guard: after Defect A, ``project_hypothesis_states_from_roots`` is the only
    faultmaven/ code path that assigns ``state = HypothesisState.VALIDATED``. A
    future autonomous writer (re-introducing the flat divergence) trips this."""
    import subprocess

    out = (
        subprocess.run(
            [
                "grep",
                "-rn",
                "state = HypothesisState.VALIDATED",
                "faultmaven/",
            ],
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .splitlines()
    )
    # Exactly one assignment, and it is inside the projection function.
    assert len(out) == 1, f"unexpected VALIDATED writers:\n{chr(10).join(out)}"
    assert "causal_graph.py" in out[0]
    src = inspect.getsource(project_hypothesis_states_from_roots)
    assert "HypothesisState.VALIDATED" in src


def test_explicit_refutation_still_flips_state_after_flat_removal():
    """Proves the explicit refutation lifecycle still carries what the removed flat
    likelihood-threshold REFUTED used to: a hypothesis refuted via the manager
    reaches REFUTED (independent of any node projection)."""
    root = _root(_nid(8), NodeState.CANDIDATE)
    hyp = _hyp(HypothesisState.ACTIVE, root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    mgr = HypothesisManager()
    mgr.refute_hypothesis(
        hypothesis=hyp,
        current_turn=case.current_turn,
        refuting_evidence_ids=[],
        reason="disproven by counterfactual",
    )
    assert hyp.state == HypothesisState.REFUTED
    # The projection leaves the explicit refutation intact.
    project_hypothesis_states_from_roots(case)
    assert hyp.state == HypothesisState.REFUTED


def test_projection_lives_next_to_node_derivation():
    # Coherence pin: the projection is defined in the causal-graph module (the
    # graph->state derivation home), not smuggled into an unrelated layer.
    assert (
        project_hypothesis_states_from_roots.__module__ == causal_graph_module.__name__
    )


# --------------------------------------------------------------------------- #
# Item 1 — progress_monitor exhaustion interaction (behavioral, no sim)
#
# Exhaustion (_detect_exhaustion) keys on "no VALIDATED hypothesis". With
# VALIDATED now graph-derived, verify the guard still reads it correctly: a
# graph-validated root SUPPRESSES the nudge, and a deeply-refuted case with only
# non-validated hypotheses gets the (advisory, guarantee-safe) handoff nudge.
# --------------------------------------------------------------------------- #


def _evidence(label: str) -> Evidence:
    return Evidence(
        evidence_id="ev_" + hashlib.md5(label.encode()).hexdigest()[:12],
        summary=f"fact-{label}",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _hyp_cat(state: HypothesisState, category: HypothesisCategory) -> Hypothesis:
    return Hypothesis(
        statement=f"theory {category.value}",
        category=category,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=1,
        rationale="r",
        state=state,
        refutation_reason=("refuted" if state == HypothesisState.REFUTED else None),
    )


def _exhausted_case(hyps) -> Case:
    case = _case(hyps=hyps)
    case.current_turn = 12
    case.turns_without_progress = 6
    case.evidence = [_evidence("a"), _evidence("b")]
    return case


def test_exhaustion_suppressed_by_a_validated_root():
    # A graph-validated root projects a VALIDATED hypothesis, which suppresses the
    # exhaustion nudge — the case has converged, not dead-ended.
    hyps = [
        _hyp_cat(HypothesisState.REFUTED, HypothesisCategory.NETWORK),
        _hyp_cat(HypothesisState.REFUTED, HypothesisCategory.DATABASE),
        _hyp_cat(HypothesisState.VALIDATED, HypothesisCategory.CONFIG),
    ]
    case = _exhausted_case(hyps)
    assert ProgressMonitor()._detect_exhaustion(case) is False


def test_deeply_refuted_case_without_validation_gets_the_nudge():
    # Accepted (intentional) behavior: with the flat false-positive VALIDATED gone,
    # a genuinely dead-ended case (broad coverage, multiple refuted, no validated
    # root) correctly receives the advisory handoff nudge. This is a nudge, not a
    # state change (guarantee-safe).
    hyps = [
        _hyp_cat(HypothesisState.REFUTED, HypothesisCategory.NETWORK),
        _hyp_cat(HypothesisState.REFUTED, HypothesisCategory.DATABASE),
        _hyp_cat(HypothesisState.ACTIVE, HypothesisCategory.CONFIG),
    ]
    case = _exhausted_case(hyps)
    assert ProgressMonitor()._detect_exhaustion(case) is True
