"""Mechanical tests for the KB cause seeder (LLM-agnostic engine-state assertions).

The seeder instantiates a retrieved runbook's ``metadata["causes"]`` chains as
CANDIDATE causal-graph nodes/hypotheses — a prior, never a conclusion. These
tests pin the engine-state contract: candidate-only, prior-capped, no evidentiary
privilege, multi-runbook dedup/compete, and — the guarantee — an unsupported
seeded prior decays and is anchoring-flagged (NO COLLAPSE, NO INCORRECT
CONCLUSION). Pass/fail is graph state, not a model judge.
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import (
    ANCHORING_SAME_CATEGORY_THRESHOLD,
    NEW_HYPOTHESIS_MAX_PRIOR,
    create_hypothesis_manager,
)
from faultmaven.core.investigation.kb_cause_seeder import (
    KB_SEED_PRIOR,
    MAX_SEEDED_CAUSES,
    SEEDED_FROM_RUNBOOK_KEY,
    SeededRunbook,
    SkipClass,
    seed_candidate_causes,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    HypothesisState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _case(with_symptom: bool = True) -> Case:
    return Case(
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
        problem_verification=(
            ProblemVerification(symptom_statement="X fails", severity=CaseSeverity.HIGH)
            if with_symptom
            else None
        ),
        current_turn=1,
    )


def _cause(
    letter: str = "A",
    *,
    root_stmt: str = "root A: the underlying fault",
    inter_stmt: str = "s1 A: the observable effect",
    fallback: bool = False,
    with_chain: bool = True,
) -> dict:
    """A per-Cause record in the shipped pack shape (root → s1 → D)."""
    if fallback or not with_chain:
        return {
            "cause_letter": letter,
            "cause_name": f"Cause {letter}",
            "cause_statement": f"cause {letter} symptom-level statement",
            "chain_nodes": [],
            "chain_edges": [],
            "rung_indicators": {},
            "interventions": [],
            "is_fallback_cause": fallback,
        }
    return {
        "cause_letter": letter,
        "cause_name": f"Cause {letter}",
        "cause_statement": f"cause {letter} symptom-level statement",
        "chain_nodes": [
            {"ref": "root", "node_type": "root", "statement": root_stmt},
            {"ref": "s1", "node_type": "intermediate", "statement": inter_stmt},
            {"ref": "D", "node_type": "problem", "statement": "X is failing"},
        ],
        "chain_edges": [
            {"cause_ref": "root", "effect_ref": "s1"},
            {"cause_ref": "s1", "effect_ref": "D"},
        ],
        "rung_indicators": {"root": [f"[Step 1] indicator for {letter}"]},
        "interventions": [
            {"ref": "root", "quadrant": "remediation", "text": "fix the root"}
        ],
        "is_fallback_cause": False,
    }


def _runbook(item_id: str, causes: list, score: float = 0.9) -> SeededRunbook:
    return SeededRunbook(item_id=item_id, score=score, causes=causes)


# ---------------------------------------------------------------------------
# Core instantiation
# ---------------------------------------------------------------------------


def test_seeds_expected_candidate_chain():
    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_cause("A")])], current_turn=1
    )

    assert report.seeded_anything
    # Nodes: root + intermediate (+ engine-seeded PROBLEM D).
    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    inters = [
        n for n in case.causal_nodes.values() if n.node_type == NodeType.INTERMEDIATE
    ]
    problems = [
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    ]
    assert len(roots) == 1 and len(inters) == 1 and len(problems) == 1

    # All candidate-only — nothing VALIDATED.
    assert all(
        n.node_state == NodeState.CANDIDATE
        for n in case.causal_nodes.values()
        if n.node_type != NodeType.PROBLEM
    )

    # Edges wire root → s1 → D.
    d_id = problems[0].node_id
    root_id = roots[0].node_id
    s1_id = inters[0].node_id
    pairs = {(e.cause_node_id, e.effect_node_id) for e in case.causal_edges}
    assert (root_id, s1_id) in pairs
    assert (s1_id, d_id) in pairs
    # Pack chains carry no and_group → OR-alternative edges.
    assert all(e.and_group is None for e in case.causal_edges)

    # One hypothesis: ACTIVE, prior-capped, root heads the path, D tails it.
    assert len(case.hypotheses) == 1
    h = next(iter(case.hypotheses.values()))
    assert h.state == HypothesisState.ACTIVE
    assert h.likelihood == KB_SEED_PRIOR
    assert h.likelihood <= NEW_HYPOTHESIS_MAX_PRIOR
    assert h.root_node_id == root_id
    assert h.path[0] == root_id and h.path[-1] == d_id
    # No VALIDATED hypothesis.
    assert all(hh.state != HypothesisState.VALIDATED for hh in case.hypotheses.values())


def test_no_evidentiary_privilege_even_if_runbook_is_certain():
    # The seeder always enters at KB_SEED_PRIOR and can never exceed the cap.
    case = _case()
    seed_candidate_causes(case, [_runbook("rb1", [_cause("A")])], current_turn=1)
    h = next(iter(case.hypotheses.values()))
    assert h.likelihood < 0.5  # below the IDENTIFIED gate — a lead, not a conclusion
    assert h.evidence_links == []  # no evidence linked by seeding


def test_fallback_cause_is_skipped():
    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_cause("Z", fallback=True)])], current_turn=1
    )
    assert not report.seeded_anything
    assert case.hypotheses == {}
    # Only the engine-seeded PROBLEM node may exist; no candidate chain.
    assert all(n.node_type == NodeType.PROBLEM for n in case.causal_nodes.values())


def test_no_problem_node_seeds_nothing():
    case = _case(with_symptom=False)
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_cause("A")])], current_turn=1
    )
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert case.causal_nodes == {}


# ---------------------------------------------------------------------------
# Caps + anchoring coupling
# ---------------------------------------------------------------------------


def test_cap_is_below_anchoring_threshold():
    # The load-bearing coupling: the seeder alone can never trip anchoring
    # condition 1 (>= threshold same-category active hypotheses).
    assert MAX_SEEDED_CAUSES < ANCHORING_SAME_CATEGORY_THRESHOLD


def test_total_causes_capped():
    # A runbook with more causes than the cap seeds at most MAX_SEEDED_CAUSES.
    many = [_cause(chr(ord("A") + i), root_stmt=f"root {i}") for i in range(6)]
    case = _case()
    report = seed_candidate_causes(case, [_runbook("rb1", many)], current_turn=1)
    assert len(report.seeded_hypothesis_ids) == MAX_SEEDED_CAUSES
    assert len(case.hypotheses) == MAX_SEEDED_CAUSES


def test_runbooks_capped():
    # More runbooks than MAX_SEEDED_RUNBOOKS: only the top ones are consulted.
    rbs = [
        _runbook("rb1", [_cause("A", root_stmt="root one")], score=0.9),
        _runbook("rb2", [_cause("B", root_stmt="root two")], score=0.8),
        _runbook("rb3", [_cause("C", root_stmt="root three")], score=0.7),
    ]
    case = _case()
    report = seed_candidate_causes(case, rbs, current_turn=1, max_runbooks=2)
    assert set(report.runbooks_used) <= {"rb1", "rb2"}
    assert "rb3" not in report.runbooks_used


# ---------------------------------------------------------------------------
# Multi-runbook merge rule
# ---------------------------------------------------------------------------


def test_identical_cause_from_two_runbooks_dedups():
    # Same root statement via two runbooks → one node, one hypothesis.
    shared = dict(root_stmt="root shared: identical fault")
    rbs = [
        _runbook("rb1", [_cause("A", **shared)], score=0.9),
        _runbook("rb2", [_cause("B", **shared)], score=0.8),
    ]
    case = _case()
    report = seed_candidate_causes(case, rbs, current_turn=1)
    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    assert len(roots) == 1  # deduped by (node_type, normalized statement)
    assert len(report.seeded_hypothesis_ids) == 1  # not double-seeded onto one root


def test_distinct_roots_compete_as_or_alternatives():
    rbs = [
        _runbook("rb1", [_cause("A", root_stmt="root alpha distinct")], score=0.9),
        _runbook("rb2", [_cause("B", root_stmt="root beta distinct")], score=0.8),
    ]
    case = _case()
    report = seed_candidate_causes(case, rbs, current_turn=1)
    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    assert len(roots) == 2
    assert len(report.seeded_hypothesis_ids) == 2
    # Competing candidates — independent OR alternatives (no AND-set).
    assert all(e.and_group is None for e in case.causal_edges)
    assert all(
        n.node_state == NodeState.CANDIDATE
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT
    )


# ---------------------------------------------------------------------------
# Provenance (read surface only)
# ---------------------------------------------------------------------------


def test_provenance_marked_on_seeded_nodes():
    case = _case()
    seed_candidate_causes(case, [_runbook("rb1", [_cause("A")])], current_turn=1)
    seeded = [
        n
        for n in case.causal_nodes.values()
        if n.metadata.get(SEEDED_FROM_RUNBOOK_KEY) == "rb1"
    ]
    # Both non-problem rungs carry provenance; the hypothesis rationale records it.
    assert len(seeded) == 2
    h = next(iter(case.hypotheses.values()))
    assert "rb1" in (h.rationale or "")


# ---------------------------------------------------------------------------
# Observable skip (4.8a): no silent drop; class-aware "contributed nothing" alarm
# ---------------------------------------------------------------------------


def test_fallback_cause_records_intentional_skip_no_alarm():
    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_cause("Z", fallback=True)])], current_turn=1
    )
    assert not report.seeded_anything
    assert len(report.skipped) == 1
    s = report.skipped[0]
    assert s.skip_class == SkipClass.INTENTIONAL
    assert s.item_id == "rb1" and s.cause_letter == "Z"
    # A fallback-only runbook is expected, not a quality problem.
    assert report.runbooks_contributing_nothing() == []


def test_malformed_cause_records_quality_drop_and_alarms():
    case = _case()
    bad = _cause("A")
    # Break the chain head: intermediate-first (no root) — a real cause the
    # seeder cannot instantiate.
    bad["chain_nodes"] = [
        {"ref": "s1", "node_type": "intermediate", "statement": "no root here"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [bad])], current_turn=1)
    assert not report.seeded_anything
    assert len(report.skipped) == 1
    assert report.skipped[0].skip_class == SkipClass.QUALITY_DROP
    # Zero-seed runbook with a genuine quality drop → the actionable alarm fires.
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_dedup_skip_is_benign_no_alarm():
    shared = dict(root_stmt="root shared: identical fault")
    rbs = [
        _runbook("rb1", [_cause("A", **shared)], score=0.9),
        _runbook("rb2", [_cause("B", **shared)], score=0.8),
    ]
    case = _case()
    report = seed_candidate_causes(case, rbs, current_turn=1)
    assert "rb1" in report.runbooks_used
    dedup = [s for s in report.skipped if s.skip_class == SkipClass.BENIGN_DEDUP]
    assert len(dedup) == 1 and dedup[0].item_id == "rb2"
    # rb2 seeded nothing, but for a benign (dedup) reason → NOT alarmed.
    assert report.runbooks_contributing_nothing() == []


def test_and_group_cause_is_rejected_not_flattened():
    # A co-necessary AND-set (edges sharing an and_group) must NOT be silently
    # seeded as OR-alternatives. The seeder rejects it as UNSUPPORTED_SHAPE.
    case = _case()
    andc = _cause("A")
    andc["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "cause A1"},
        {"ref": "r2", "node_type": "root", "statement": "cause A2"},
        {"ref": "s1", "node_type": "intermediate", "statement": "joint effect"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    # A1 AND A2 are both required to produce s1 (same effect + same and_group).
    andc["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1", "and_group": "g1"},
        {"cause_ref": "r2", "effect_ref": "s1", "and_group": "g1"},
        {"cause_ref": "s1", "effect_ref": "D"},
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [andc])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert len(report.skipped) == 1
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    # An unmodeled real cause that seeds nothing IS actionable (build AND support).
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_runbook_that_seeded_something_is_not_alarmed_despite_a_quality_drop():
    good = _cause("A", root_stmt="good root cause")
    bad = _cause("B")
    bad["chain_nodes"] = []  # no nodes → quality drop
    case = _case()
    report = seed_candidate_causes(case, [_runbook("rb1", [good, bad])], current_turn=1)
    assert report.seeded_anything and "rb1" in report.runbooks_used
    assert any(s.skip_class == SkipClass.QUALITY_DROP for s in report.skipped)
    # rb1 contributed a candidate, so it is not flagged as "contributed nothing".
    assert report.runbooks_contributing_nothing() == []


# ---------------------------------------------------------------------------
# GUARANTEE: misleading runbook — decay + anchoring, no collapse
# ---------------------------------------------------------------------------


def test_misleading_seed_decays_and_is_anchoring_flagged():
    """A wrong seeded prior with no supporting evidence decays and is
    anchoring-flagged — the engine does not conclude on it."""
    case = _case()
    hm = create_hypothesis_manager()
    seed_candidate_causes(
        case, [_runbook("rb1", [_cause("A")])], current_turn=1, hypothesis_manager=hm
    )
    h = next(iter(case.hypotheses.values()))
    start = h.likelihood

    # Simulate stalled turns (no evidence links the seed): the counter climbs and
    # decay compounds — exactly the self-generated stagnation path.
    for turn in range(2, 6):
        h.iterations_without_progress += 1
        hm.apply_likelihood_decay(h, turn)

    assert h.likelihood < start  # decayed, no evidentiary privilege

    # Anchoring detection sees the stalled low-confidence seed (condition 3).
    is_anchored, reason, ids = hm.detect_anchoring(list(case.hypotheses.values()), 6)
    assert is_anchored
    assert h.hypothesis_id in ids

    # No collapse: nothing VALIDATED, no root-cause conclusion asserted.
    assert all(n.node_state != NodeState.VALIDATED for n in case.causal_nodes.values())
    assert getattr(case, "root_cause_conclusion", None) in (None, "")


# ---------------------------------------------------------------------------
# GUARANTEE: provenance-blindness invariant
# ---------------------------------------------------------------------------


def test_safety_mechanisms_are_provenance_blind():
    """No safety mechanism (confidence decay, anchoring detection, failed-fix
    demotion, node/hypothesis state derivation, cause_state derivation) may
    branch on seeded provenance — that is what keeps a seed mechanically
    indistinguishable from a self-generated hypothesis.

    These mechanisms live in causal_graph.py + hypothesis_manager.py (decay /
    anchoring / demotion / node+hypothesis state derivation) and milestone_engine.py
    (cause_state derivation + the per-turn housekeeping loop). Assert the
    provenance key never appears in any of them, so a future edit cannot quietly
    grant a seed evidentiary weight.

    INVARIANT MAINTENANCE: this module set must track any move of safety logic.
    If decay/anchoring/demotion/state-derivation is relocated to another module,
    add that module here — a whole-file grep is deliberately coarse so the guard
    can never be silently narrowed below where the safety logic actually lives.
    """
    import faultmaven.core.investigation.causal_graph as cg
    import faultmaven.core.investigation.hypothesis_manager as hmmod
    import faultmaven.core.investigation.milestone_engine as engine

    for module in (cg, hmmod, engine):
        with open(module.__file__, "r", encoding="utf-8") as fh:
            source = fh.read()
        assert SEEDED_FROM_RUNBOOK_KEY not in source, (
            f"{module.__name__} references the seeded-provenance key — a safety "
            "mechanism must never branch on origin"
        )
