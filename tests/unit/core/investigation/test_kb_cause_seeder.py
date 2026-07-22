"""Mechanical tests for the KB cause seeder (LLM-agnostic engine-state assertions).

The seeder instantiates a retrieved runbook's ``metadata["causes"]`` chains as
CANDIDATE causal-graph nodes/hypotheses — a prior, never a conclusion. These
tests pin the engine-state contract: candidate-only, prior-capped, no evidentiary
privilege, multi-runbook dedup/compete, and — the guarantee — an unsupported
seeded prior decays and is anchoring-flagged (NO COLLAPSE, NO INCORRECT
CONCLUSION). Pass/fail is graph state, not a model judge.
"""

from datetime import UTC, datetime
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
    SEEDED_INTERVENTIONS_KEY,
    SEEDED_RATIONALE_PREFIX,
    SeededRunbook,
    SkipClass,
    _emit_rung_needs,
    _sanitize_interventions,
    confirmed_cause_interventions,
    confirmed_root_seed_origin,
    seed_candidate_causes,
)
from faultmaven.core.investigation.milestone_engine import (
    _supersede_needs_on_hypothesis_retirement,
)
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
    HypothesisCategory,
    HypothesisState,
    InquiryData,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
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
            "cause_statement": f"{root_stmt} symptom-level statement",
            "chain_nodes": [],
            "chain_edges": [],
            "rung_indicators": {},
            "interventions": [],
            "is_fallback_cause": fallback,
        }
    return {
        "cause_letter": letter,
        "cause_name": f"Cause {letter}",
        "cause_statement": f"{root_stmt} symptom-level statement",
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


def test_shared_root_divergent_chain_dedups_without_orphan_nodes():
    # Two runbooks share a root statement but diverge mid-chain. The second is a
    # BENIGN_DEDUP — and because the dedup is decided BEFORE ingest, the second
    # runbook's divergent intermediate must NEVER be minted as an orphan node.
    rb1 = _runbook(
        "rb1",
        [_cause("A", root_stmt="shared root fault", inter_stmt="first path effect")],
        score=0.9,
    )
    rb2 = _runbook(
        "rb2",
        [_cause("B", root_stmt="shared root fault", inter_stmt="second path effect")],
        score=0.8,
    )
    case = _case()
    report = seed_candidate_causes(case, [rb1, rb2], current_turn=1)

    # rb1 seeded; rb2 deduped (benign, not alarmed).
    assert report.runbooks_used == ["rb1"]
    dedup = [s for s in report.skipped if s.skip_class == SkipClass.BENIGN_DEDUP]
    assert len(dedup) == 1 and dedup[0].item_id == "rb2"
    assert report.runbooks_contributing_nothing() == []

    # The divergent intermediate ("second path effect") was never minted.
    statements = {n.statement for n in case.causal_nodes.values()}
    assert "second path effect" not in statements

    # Orphan-free invariant: every non-problem node lies on some hypothesis path.
    on_a_path = {nid for h in case.hypotheses.values() for nid in (h.path or [])}
    non_problem_ids = {
        nid for nid, n in case.causal_nodes.items() if n.node_type != NodeType.PROBLEM
    }
    assert non_problem_ids <= on_a_path, "a seeded node is orphaned (off every path)"


def test_cause_that_raises_is_recorded_as_skip_and_pass_continues():
    # A cause that makes _seed_one_cause raise (malformed non-list chain_nodes)
    # must not abort the pass or discard the report: it is recorded as a skip and
    # the remaining causes still seed.
    case = _case()
    boom = _cause("B")
    boom["chain_nodes"] = "not-a-list"  # str → n.get() raises inside _seed_one_cause
    good = _cause("A", root_stmt="good root cause")
    report = seed_candidate_causes(
        case, [_runbook("rb1", [boom, good])], current_turn=1
    )
    assert report.seeded_anything  # the good cause still seeded
    assert "rb1" in report.runbooks_used
    raised = [s for s in report.skipped if s.cause_letter == "B"]
    assert len(raised) == 1 and raised[0].skip_class == SkipClass.QUALITY_DROP


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


def test_second_root_mid_chain_is_rejected_not_seeded():
    # A chain with a second root (head IS a root, so the head-is-root check
    # passes) is two chains, not one linear path — reject as UNSUPPORTED_SHAPE
    # rather than mis-seed it as a linear chain.
    case = _case()
    two_roots = _cause("A")
    two_roots["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "root one"},
        {"ref": "r2", "node_type": "root", "statement": "root two"},
        {"ref": "s1", "node_type": "intermediate", "statement": "shared effect"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    two_roots["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},
        {"cause_ref": "r2", "effect_ref": "s1"},
        {"cause_ref": "s1", "effect_ref": "D"},
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [two_roots])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_branching_fork_is_rejected_not_linearized():
    # A rung that produces two distinct effects (a fork) must NOT be silently
    # flattened to one arbitrary branch (last-edge-wins). Reject it.
    case = _case()
    fork = _cause("A")
    fork["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "forking root"},
        {"ref": "s1", "node_type": "intermediate", "statement": "branch one"},
        {"ref": "s2", "node_type": "intermediate", "statement": "branch two"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    fork["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},
        {"cause_ref": "root", "effect_ref": "s2"},  # root forks — two effects
        {"cause_ref": "s1", "effect_ref": "D"},
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [fork])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_convergence_join_without_and_group_is_rejected():
    # A rung produced by two distinct causes (a merge / convergence) without an
    # and_group is not a single linear chain — reject it (single root, so the
    # multiple-roots guard does NOT fire; the repeated-effect_ref guard must).
    case = _case()
    join = _cause("A")
    join["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "converge root"},
        {"ref": "x", "node_type": "intermediate", "statement": "second producer"},
        {"ref": "s1", "node_type": "intermediate", "statement": "merge point"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    join["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},
        {"cause_ref": "x", "effect_ref": "s1"},  # s1 produced by two causes — a join
        {"cause_ref": "s1", "effect_ref": "D"},
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [join])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_dangling_edge_ref_is_rejected_not_seeded():
    # An edge whose effect_ref resolves to no node would silently disconnect a
    # rung. Reject rather than mis-seed a broken chain.
    case = _case()
    dangling = _cause("A")
    dangling["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "dangling root"},
        {"ref": "s1", "node_type": "intermediate", "statement": "orphaned effect"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    dangling["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},
        {"cause_ref": "s1", "effect_ref": "ghost"},  # no such node
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [dangling])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    assert report.runbooks_contributing_nothing() == ["rb1"]


@pytest.mark.parametrize("bad_ref", [None, ""])
def test_null_or_empty_node_ref_is_rejected_not_seeded(bad_ref):
    # A non-problem node with a missing/None or empty ref is a live produce-path
    # malformation (curated packs always ref; LLM-authored conversions may not).
    # It silently poisons resolution: ref_to_index keys None/"" as a valid node,
    # so the edge resolve-checks and reachability walk pass, but produces_by_ref
    # then drops the null-keyed edge — minting a disconnected/self-referential
    # seed. The head-is-root check passes (node_type is root), so the shape guard
    # must reject it explicitly rather than mis-seed.
    case = _case()
    bad = _cause("A")
    bad["chain_nodes"] = [
        {"ref": bad_ref, "node_type": "root", "statement": "bad-ref root"},
        {"ref": "s1", "node_type": "intermediate", "statement": "effect"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    bad["chain_edges"] = [
        {"cause_ref": bad_ref, "effect_ref": "s1"},
        {"cause_ref": "s1", "effect_ref": "D"},
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [bad])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    # Only the engine-seeded PROBLEM D may exist; no root/intermediate was minted.
    assert not any(n.node_type != NodeType.PROBLEM for n in case.causal_nodes.values())
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_convergence_onto_d_via_ref_alias_is_rejected():
    # A join whose two producers both terminate at the case D node — one via the
    # literal "D", one via the problem node's own ref — is still a convergence.
    # "D" and every problem ref denote the one D node, so the merge check must see
    # through the aliasing (this is the shape the raw-literal check missed).
    case = _case()
    join = _cause("A")
    join["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "alias-join root"},
        {"ref": "s1", "node_type": "intermediate", "statement": "producer one"},
        {"ref": "s2", "node_type": "intermediate", "statement": "producer two"},
        {"ref": "P", "node_type": "problem", "statement": "X is failing"},
    ]
    join["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},
        {"cause_ref": "s1", "effect_ref": "D"},  # produces D via the literal
        {"cause_ref": "s2", "effect_ref": "P"},  # produces D via the problem ref
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [join])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_chain_not_terminating_at_d_is_rejected():
    # Every ref appears at most once, so the fork/join/dangling checks all pass —
    # but the root's path dead-ends at s1 and never reaches D. A chain that does
    # not terminate at the problem is not a root→…→D path; reject it.
    case = _case()
    stub = _cause("A")
    stub["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "truncated root"},
        {"ref": "s1", "node_type": "intermediate", "statement": "dead-end rung"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    stub["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},  # ends here, never reaches D
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [stub])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_disconnected_rung_off_the_root_to_d_path_is_rejected():
    # A rung not on the root→D route (here a self-referential cycle on s2, with the
    # root's own path reaching D independently) passes every ≤once edge check but
    # is not a single linear chain. The reachability walk must reject it.
    case = _case()
    frag = _cause("A")
    frag["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "connected root"},
        {"ref": "s1", "node_type": "intermediate", "statement": "on-path rung"},
        {"ref": "s2", "node_type": "intermediate", "statement": "off-path rung"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    frag["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},
        {"cause_ref": "s1", "effect_ref": "D"},
        {"cause_ref": "s2", "effect_ref": "s2"},  # cycle/fragment off the root path
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [frag])], current_turn=1)
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert report.skipped[0].skip_class == SkipClass.UNSUPPORTED_SHAPE
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_linear_chain_terminating_via_problem_ref_still_seeds():
    # The valid counterpart to the alias-join rejection: a well-formed linear chain
    # whose final edge points at the problem node's ref (not the literal "D") must
    # still seed. Canonicalizing D must not reject legitimate chains.
    case = _case()
    good = _cause("A")
    good["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": "well-formed root"},
        {"ref": "s1", "node_type": "intermediate", "statement": "the effect"},
        {"ref": "P", "node_type": "problem", "statement": "X is failing"},
    ]
    good["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},
        {"cause_ref": "s1", "effect_ref": "P"},  # terminates at D via the problem ref
    ]
    report = seed_candidate_causes(case, [_runbook("rb1", [good])], current_turn=1)
    assert report.seeded_anything
    assert len(case.hypotheses) == 1


def test_runbook_that_seeded_something_is_not_alarmed_despite_a_quality_drop():
    good = _cause("A", root_stmt="good root cause")
    bad = _cause("B")
    # A genuine quality drop: an intermediate-first chain (no root) the seeder
    # cannot instantiate. (An *empty* chain is no longer a drop — a chain-less
    # cause with a Statement synthesizes a degenerate root→D; see the
    # chain-less-synthesis tests below.)
    bad["chain_nodes"] = [
        {"ref": "s1", "node_type": "intermediate", "statement": "no root here"},
        {"ref": "D", "node_type": "problem", "statement": "X is failing"},
    ]
    bad["chain_edges"] = [{"cause_ref": "s1", "effect_ref": "D"}]
    case = _case()
    report = seed_candidate_causes(case, [_runbook("rb1", [good, bad])], current_turn=1)
    assert report.seeded_anything and "rb1" in report.runbooks_used
    assert any(s.skip_class == SkipClass.QUALITY_DROP for s in report.skipped)
    # rb1 contributed a candidate, so it is not flagged as "contributed nothing".
    assert report.runbooks_contributing_nothing() == []


# ---------------------------------------------------------------------------
# Cross-chain convergence (grammar-legal `converges:` directive) — a NON-actionable
# reject that must NOT trip the quality alarm
# ---------------------------------------------------------------------------


def _converges_cause(letter: str = "A", target: str = "B.s1") -> dict:
    """A cause whose chain terminates in a `converges: <Cause>.<ref>` directive —
    the shape BOTH producers emit: a chain edge carrying a truthy ``converges`` key
    whose ``effect_ref`` points into another Cause's chain (no self-contained D)."""
    cause = _cause(letter)
    cause["chain_nodes"] = [
        {"ref": "root", "node_type": "root", "statement": f"converging root {letter}"},
        {"ref": "s1", "node_type": "intermediate", "statement": f"effect {letter}"},
    ]
    cause["chain_edges"] = [
        {"cause_ref": "root", "effect_ref": "s1"},
        {"cause_ref": "s1", "effect_ref": target, "converges": True},
    ]
    return cause


def test_converges_edge_rejected_as_converges_unmodeled_no_alarm():
    # A grammar-legal cross-chain convergence is rejected under its OWN skip class
    # (not UNSUPPORTED_SHAPE, which would fire the quality alarm on a well-authored
    # runbook). It seeds nothing and is NOT alarmed.
    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_converges_cause("A")])], current_turn=1
    )
    assert not report.seeded_anything
    assert case.hypotheses == {}
    assert len(report.skipped) == 1
    assert report.skipped[0].skip_class == SkipClass.CONVERGES_UNMODELED
    # A converges directive is legal grammar, not a quality drop → no alarm.
    assert report.runbooks_contributing_nothing() == []


def test_converges_cause_detected_before_dangling_ref_misdiagnosis():
    # The convergence edge's effect_ref ("B.s1") resolves to no node in THIS chain;
    # were it not caught first, _reject_nonlinear_shape would mis-class it as a
    # dangling ref (UNSUPPORTED_SHAPE, alarmed). The converges class proves it is
    # caught before that misdiagnosis.
    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_converges_cause("A", target="Q.x")])], current_turn=1
    )
    assert report.skipped[0].skip_class == SkipClass.CONVERGES_UNMODELED
    assert not any(s.skip_class == SkipClass.UNSUPPORTED_SHAPE for s in report.skipped)


def test_converges_cause_alongside_linear_cause_still_seeds_the_linear_one():
    # A runbook with one converges cause + one ordinary linear cause seeds the
    # linear one; the converges cause is a non-actionable skip, and the runbook —
    # having contributed a candidate — is not alarmed.
    case = _case()
    linear = _cause("B", root_stmt="ordinary linear root fault")
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_converges_cause("A"), linear])], current_turn=1
    )
    assert report.seeded_anything and "rb1" in report.runbooks_used
    assert len(case.hypotheses) == 1  # only the linear cause seeded
    classes = {s.skip_class for s in report.skipped}
    assert SkipClass.CONVERGES_UNMODELED in classes
    assert report.runbooks_contributing_nothing() == []


# ---------------------------------------------------------------------------
# Paraphrase dedup — a second runbook whose cause paraphrases an already-seeded
# hypothesis is skipped BENIGN_DEDUP (reusing the INV-36 predicate), never minting
# a phantom OR-sibling; fail-open guards keep genuinely distinct siblings
# ---------------------------------------------------------------------------


def _cause_stmt(letter: str, *, root_stmt: str, statement: str) -> dict:
    """A cause whose ROOT statement and hypothesis-level ``cause_statement`` are set
    independently — so a test can make two runbooks' *hypothesis* statements
    paraphrase each other while their root statements stay distinct (isolating the
    paraphrase-dedup path from the exact-normalized-root dedup)."""
    cause = _cause(letter, root_stmt=root_stmt)
    cause["cause_statement"] = statement
    return cause


def test_paraphrase_of_seeded_hypothesis_is_benign_dedup_no_second_hypothesis():
    # Two runbooks describe one cause in reworded form: the hypothesis statements
    # are mutual mirrors above the INV-36 bar, but the ROOTS differ (so the exact
    # root dedup does NOT fire — the paraphrase check must). The second is skipped
    # BENIGN_DEDUP, mints no second hypothesis, and — decided before ingest —
    # leaves no orphan nodes.
    rb1 = _runbook(
        "rb1",
        [
            _cause_stmt(
                "A",
                root_stmt="alpha root one",
                statement="connection pool exhausted under heavy load",
            )
        ],
        score=0.9,
    )
    rb2 = _runbook(
        "rb2",
        [
            _cause_stmt(
                "B",
                root_stmt="beta root two",
                statement="under heavy load the connection pool exhausted",
            )
        ],
        score=0.8,
    )
    case = _case()
    report = seed_candidate_causes(case, [rb1, rb2], current_turn=1)

    assert len(report.seeded_hypothesis_ids) == 1  # rb2's paraphrase did not seed
    assert len(case.hypotheses) == 1
    dedup = [s for s in report.skipped if s.skip_class == SkipClass.BENIGN_DEDUP]
    assert len(dedup) == 1 and dedup[0].item_id == "rb2"
    assert "paraphrase" in dedup[0].reason
    # No alarm — a paraphrase overlap is expected non-seeding.
    assert report.runbooks_contributing_nothing() == []
    # Orphan-free: every non-problem node lies on the one hypothesis's path.
    on_path = set()
    for h in case.hypotheses.values():
        on_path.update(h.path or [])
    for n in case.causal_nodes.values():
        if n.node_type != NodeType.PROBLEM:
            assert n.node_id in on_path


def test_negated_paraphrase_fails_open_and_seeds_a_distinct_sibling():
    # The polarity guard fails open: a second cause that NEGATES the first is a
    # dispute, not a duplicate — it must seed as a distinct OR-sibling.
    rb1 = _runbook(
        "rb1",
        [
            _cause_stmt(
                "A",
                root_stmt="alpha root one",
                statement="the connection pool exhausted under heavy load",
            )
        ],
        score=0.9,
    )
    rb2 = _runbook(
        "rb2",
        [
            _cause_stmt(
                "B",
                root_stmt="beta root two",
                statement="the connection pool not exhausted under heavy load",
            )
        ],
        score=0.8,
    )
    case = _case()
    report = seed_candidate_causes(case, [rb1, rb2], current_turn=1)
    assert len(report.seeded_hypothesis_ids) == 2  # distinct siblings, both seeded
    assert len(case.hypotheses) == 2
    assert not any(s.skip_class == SkipClass.BENIGN_DEDUP for s in report.skipped)


def test_numeric_discriminator_paraphrase_fails_open_and_seeds_distinct_sibling():
    # The numeric-discriminator guard fails open: two causes differing only by a
    # number the token mirror drops ("server 1" vs "server 2") stay distinct.
    rb1 = _runbook(
        "rb1",
        [
            _cause_stmt(
                "A",
                root_stmt="alpha root one",
                statement="server 1 connection pool dropped",
            )
        ],
        score=0.9,
    )
    rb2 = _runbook(
        "rb2",
        [
            _cause_stmt(
                "B",
                root_stmt="beta root two",
                statement="server 2 connection pool dropped",
            )
        ],
        score=0.8,
    )
    case = _case()
    report = seed_candidate_causes(case, [rb1, rb2], current_turn=1)
    assert len(report.seeded_hypothesis_ids) == 2
    assert len(case.hypotheses) == 2
    assert not any(s.skip_class == SkipClass.BENIGN_DEDUP for s in report.skipped)


def test_chainless_standing_hypothesis_does_not_suppress_structural_seed():
    # The paraphrase dedup is scoped to CHAIN-HEADING hypotheses. A chain-less
    # standing hypothesis (root_node_id unset) whose statement paraphrases the
    # runbook cause must NOT suppress it — suppressing a structurally-rich cause
    # would silently discard its chain, rung-indicator evidence-needs, and
    # interventions, classed benign and never surfaced. So the cause still seeds.
    case = _case()
    hm = create_hypothesis_manager()
    standing = hm.create_hypothesis(
        statement="connection pool exhausted under heavy load",
        category=HypothesisCategory.OTHER,
        initial_likelihood=0.3,
        current_turn=1,
    )
    assert not standing.root_node_id  # chain-less: heads no chain
    case.hypotheses[standing.hypothesis_id] = standing

    cause = _cause_stmt(
        "A",
        root_stmt="alpha root one",
        statement="under heavy load the connection pool exhausted",
    )
    report = seed_candidate_causes(
        case, [_runbook("rb1", [cause])], current_turn=1, hypothesis_manager=hm
    )

    # Seeded despite paraphrasing the chain-less standing hypothesis.
    assert report.seeded_anything
    assert not any(s.skip_class == SkipClass.BENIGN_DEDUP for s in report.skipped)
    # A new chain-heading hypothesis (the seed) now exists alongside the standing one.
    chain_heading = [h for h in case.hypotheses.values() if h.root_node_id]
    assert len(chain_heading) == 1
    assert len(case.hypotheses) == 2


# ---------------------------------------------------------------------------
# Chain-less cause — degenerate root→D synthesis (produce-path flywheel)
# ---------------------------------------------------------------------------
#
# RULE-4 makes **Chain** optional for a simple one-step cause; the v4 grammar
# declares its absence "yields a degenerate root → D chain on ingestion". Such a
# cause names its root directly in **Statement**, so the seeder synthesizes
# root→D and seeds ONE candidate rather than dropping it. 0/640 in the shipped
# pack (every real cause is chained); this is exercised only by the produce side.


def test_chainless_cause_synthesizes_root_to_d_and_seeds():
    case = _case()
    cause = _cause("A", with_chain=False)  # no chain, but carries a Statement
    assert cause["chain_nodes"] == []
    report = seed_candidate_causes(case, [_runbook("rb1", [cause])], current_turn=1)

    assert report.seeded_anything
    # Exactly one root minted (from the Statement), wired to the engine-seeded D.
    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    problems = [
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    ]
    inters = [
        n for n in case.causal_nodes.values() if n.node_type == NodeType.INTERMEDIATE
    ]
    assert len(roots) == 1 and len(problems) == 1 and inters == []
    pairs = {(e.cause_node_id, e.effect_node_id) for e in case.causal_edges}
    assert (roots[0].node_id, problems[0].node_id) in pairs

    # One CANDIDATE hypothesis at the seed prior — a lead, never a conclusion.
    assert len(case.hypotheses) == 1
    h = next(iter(case.hypotheses.values()))
    assert h.state == HypothesisState.ACTIVE
    assert h.likelihood == KB_SEED_PRIOR
    assert h.root_node_id == roots[0].node_id
    assert h.path[-1] == problems[0].node_id
    assert roots[0].node_state == NodeState.CANDIDATE
    # Provenance recorded (read surface only).
    assert roots[0].metadata.get(SEEDED_FROM_RUNBOOK_KEY) == "rb1"


def test_chainless_cause_does_not_trip_contributed_nothing_alarm():
    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_cause("A", with_chain=False)])], current_turn=1
    )
    # A chain-less cause seeds, so its runbook is not falsely alarmed.
    assert report.runbooks_contributing_nothing() == []


def test_chainless_cause_without_statement_still_quality_drops():
    # A cause with neither a chain nor a Statement is genuinely uninstantiable —
    # synthesis needs the Statement as the root, so this remains a QUALITY_DROP.
    case = _case()
    empty = _cause("A", with_chain=False)
    empty["cause_statement"] = ""
    report = seed_candidate_causes(case, [_runbook("rb1", [empty])], current_turn=1)
    assert not report.seeded_anything
    assert report.skipped[0].skip_class == SkipClass.QUALITY_DROP
    assert report.runbooks_contributing_nothing() == ["rb1"]


def test_chainless_synthesized_seed_is_candidate_only_never_validated():
    # The synthesized chain is subject to the same soundness invariant: a seeded
    # prior can never be VALIDATED without case evidence.
    case = _case()
    seed_candidate_causes(
        case, [_runbook("rb1", [_cause("A", with_chain=False)])], current_turn=1
    )
    assert all(h.state != HypothesisState.VALIDATED for h in case.hypotheses.values())
    assert all(
        n.node_state == NodeState.CANDIDATE
        for n in case.causal_nodes.values()
        if n.node_type != NodeType.PROBLEM
    )


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
# confirmed_root_seed_origin — provenance-based runbook uniqueness (Phase 5.2b)
# ---------------------------------------------------------------------------


def _seed_case_with_root(
    item_id: str = "rb_seed_1", root_stmt: str = "root A: the underlying fault"
) -> "tuple[Case, str]":
    """Seed one runbook cause and return (case, seeded_root_node_id)."""
    case = _case()
    hm = create_hypothesis_manager()
    seed_candidate_causes(
        case,
        [_runbook(item_id, [_cause("A", root_stmt=root_stmt)])],
        current_turn=1,
        hypothesis_manager=hm,
    )
    root_id = next(
        nid
        for nid, node in case.causal_nodes.items()
        if node.node_type == NodeType.ROOT
        and SEEDED_FROM_RUNBOOK_KEY in (node.metadata or {})
    )
    return case, root_id


def _add_unmarked_root(case: "Case", statement: str) -> str:
    """A self-discovered ROOT node — no seed provenance marker."""
    node = CausalNode(statement=statement, node_type=NodeType.ROOT, generated_at_turn=1)
    case.causal_nodes[node.node_id] = node
    return node.node_id


def _confirm_root(case: "Case", node_id: str) -> None:
    """Make a root counterfactually CONFIRMED: VALIDATED + a SUPPORTS link to a
    causal_absence row (the gone⇒gone proof), mirroring the resolution stamp."""
    row = Evidence(
        summary="post-fix verification: cause no longer present",
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_at=datetime.now(UTC),
        collected_by="u",
        primary_purpose="confirm root cause eliminated",
        preprocessed_content="cause absent after fix",
        content_size_bytes=40,
        preprocessing_method="manual",
        collected_at_turn=2,
    )
    case.evidence.append(row)
    node = case.causal_nodes[node_id]
    node.node_state = NodeState.VALIDATED
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=row.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning="removing the cause removed the problem",
            linked_at_turn=2,
        )
    )


def test_origin_returned_when_confirmed_root_was_seeded():
    """A case resolved by validating the seeded cause resolves to its runbook —
    the direct 'this case duplicates runbook X' signal."""
    case, root_id = _seed_case_with_root("rb_argocd")
    _confirm_root(case, root_id)
    assert confirmed_root_seed_origin(case) == "rb_argocd"


def test_none_when_no_confirmed_root():
    """A seeded candidate that never validated is not a resolution — no origin
    (candidate-only seeds must never suppress a future offer)."""
    case, _root_id = _seed_case_with_root("rb_argocd")
    # Root left CANDIDATE — the seeder never validates.
    assert confirmed_root_seed_origin(case) is None


def test_none_when_confirmed_cause_was_self_discovered():
    """The decisive R3 distinction vs ``case_has_seeded_candidates``: a case can
    carry seeded candidates the LLM refuted while resolving a DIFFERENT,
    self-discovered cause. That case must still be offered a runbook."""
    case, _seeded_root = _seed_case_with_root("rb_unrelated")
    # A distinct self-discovered cause (no shared tokens, no path) → its own
    # cluster, no seed marker.
    own = _add_unmarked_root(case, "east-region network partition dropped traffic")
    _confirm_root(case, own)
    assert confirmed_root_seed_origin(case) is None


def test_origin_returned_via_cluster_when_seeded_duplicate_confirmed():
    """Clustering ranges over ALL roots: a self-discovered confirmed root that is
    a DUPLICATE (mutual mirror) of a seeded candidate collapses onto it, so the
    origin is still found even though the confirmed node carries no marker."""
    stmt = "database connection pool exhausted under load"
    case, _seeded_root = _seed_case_with_root("rb_dbpool", root_stmt=stmt)
    # Self-discovered restatement of the same cause (the LLM re-emitted it as a
    # fresh node instead of referencing the seeded one) — mutual mirror merges.
    dup = _add_unmarked_root(case, stmt)
    _confirm_root(case, dup)
    assert confirmed_root_seed_origin(case) == "rb_dbpool"


def test_refuted_seeded_root_does_not_claim_resolution():
    """A seeded root REFUTED by a failed fix must never be the basis for a
    'resolved by applying X' signal, even when it would cluster (mutual mirror)
    with a later-confirmed root — a disproven seed did not resolve the case, and
    a refuted start node would otherwise poison the cluster via the descendant
    walk's VALIDATED/count-held precondition."""
    stmt = "database connection pool exhausted under load"
    case, seeded_root = _seed_case_with_root("rb_disproven", root_stmt=stmt)
    case.causal_nodes[seeded_root].node_state = NodeState.REFUTED
    # The real, self-discovered cause (a mirror statement) is what got confirmed.
    real = _add_unmarked_root(case, stmt)
    _confirm_root(case, real)
    assert confirmed_root_seed_origin(case) is None


def test_none_on_empty_graph():
    case = _case()
    assert confirmed_root_seed_origin(case) is None


def _make_conversion_ready(case: "Case") -> None:
    """Add the RCC record + actionable solution a CONFIRMED case needs to clear
    ``runbook_conversion_ready`` (problem definition already comes from ``_case``)."""
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the underlying fault",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.85,
        mechanism="fault propagates to the observed failure",
    )
    case.solutions.append(
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Apply the fix",
            longterm_fix="reconfigure and redeploy",
        )
    )


def test_offer_gate_end_to_end_suppresses_seeded_resolution():
    """End-to-end through the REAL helper: a runbook-conversion-ready case whose
    CONFIRMED cause was seeded is NOT offered the generate affordance — pinning
    the wiring between the helper and its offer-gate reader."""
    from faultmaven.core.investigation.cause_assurance import runbook_conversion_ready
    from faultmaven.core.investigation.milestone_engine import _runbook_suggestion

    case, root_id = _seed_case_with_root("rb_e2e")
    _confirm_root(case, root_id)
    _make_conversion_ready(case)

    # The case is genuinely convertible — only provenance suppresses it.
    assert runbook_conversion_ready(case) is True
    assert confirmed_root_seed_origin(case) == "rb_e2e"
    assert _runbook_suggestion(case) is None


def test_offer_gate_end_to_end_offers_self_discovered_resolution():
    """The mirror: a runbook-conversion-ready case whose CONFIRMED cause was
    self-discovered (no seed) IS offered the affordance."""
    from faultmaven.core.investigation.cause_assurance import runbook_conversion_ready
    from faultmaven.core.investigation.milestone_engine import _runbook_suggestion

    case = _case()
    own = _add_unmarked_root(case, "east-region network partition dropped traffic")
    _confirm_root(case, own)
    _make_conversion_ready(case)

    assert runbook_conversion_ready(case) is True
    assert confirmed_root_seed_origin(case) is None
    assert _runbook_suggestion(case) is not None


# ---------------------------------------------------------------------------
# R9: interventions -> seed-time capture + confirmed-cause read helper
# ---------------------------------------------------------------------------


def test_sanitize_interventions_keeps_wellformed_drops_junk():
    """The verbatim metadata["causes"] interventions list is normalized: only
    dict entries with non-empty text survive; fields are coerced to bounded
    strings; a non-list input yields []."""
    assert _sanitize_interventions("not a list") == []
    assert _sanitize_interventions(None) == []
    out = _sanitize_interventions(
        [
            {"quadrant": "remediation", "ref": "root", "text": "  fix the root  "},
            "bad",  # not a dict
            {"text": ""},  # empty text
            {"quadrant": "mitigation", "text": "buy time"},  # missing ref ok
            {"no": "text key"},  # no text
        ]
    )
    assert out == [
        {"quadrant": "remediation", "ref": "root", "text": "fix the root"},
        {"quadrant": "mitigation", "ref": "", "text": "buy time"},
    ]


def test_interventions_captured_on_seeded_root():
    """Seeding a cause stashes its interventions on the freshly-minted ROOT node
    (read surface for the SOLUTION-stage render), not on the intermediate."""
    case, root_id = _seed_case_with_root("rb_iv")
    root = case.causal_nodes[root_id]
    captured = (root.metadata or {}).get(SEEDED_INTERVENTIONS_KEY)
    assert captured == [
        {"quadrant": "remediation", "ref": "root", "text": "fix the root"}
    ]
    # No intermediate carries the interventions surface — root-only.
    for nid, node in case.causal_nodes.items():
        if node.node_type != NodeType.ROOT:
            assert SEEDED_INTERVENTIONS_KEY not in (node.metadata or {})


def test_interventions_not_captured_on_reused_self_generated_root():
    """A reused (self-generated) root must stay origin-free: neither the runbook
    marker nor the interventions surface may be stamped onto it — same discipline
    as the SEEDED_FROM_RUNBOOK_KEY provenance stamp."""
    stmt = "root A: the underlying fault"
    case = _case()
    reused = _add_unmarked_root(case, stmt)  # exists but heads no hypothesis
    seed_candidate_causes(
        case, [_runbook("rb_reuse", [_cause("A", root_stmt=stmt)])], current_turn=1
    )
    node = case.causal_nodes[reused]
    assert SEEDED_INTERVENTIONS_KEY not in (node.metadata or {})
    assert SEEDED_FROM_RUNBOOK_KEY not in (node.metadata or {})


def test_malformed_interventions_do_not_break_seeding():
    """A malformed interventions value must not fail seeding — the candidate is
    still seeded, just without a captured interventions surface."""
    cause = _cause("A")
    cause["interventions"] = "not a list at all"
    case = _case()
    report = seed_candidate_causes(case, [_runbook("rb_bad", [cause])], current_turn=1)
    assert report.seeded_anything
    root = next(n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT)
    assert SEEDED_INTERVENTIONS_KEY not in (root.metadata or {})


def test_confirmed_cause_interventions_returned_when_confirmed():
    case, root_id = _seed_case_with_root("rb_iv")
    _confirm_root(case, root_id)
    assert confirmed_cause_interventions(case) == [
        {"quadrant": "remediation", "ref": "root", "text": "fix the root"}
    ]


def test_confirmed_cause_interventions_empty_when_only_candidate():
    """A seeded candidate that never validated yields no interventions — the
    render only fires once the cause is actually confirmed."""
    case, _root_id = _seed_case_with_root("rb_iv")
    assert confirmed_cause_interventions(case) == []


def test_confirmed_cause_interventions_empty_when_self_discovered():
    """A case resolved on a self-discovered cause (with a refuted seed for a
    different cause present) surfaces no seeded interventions."""
    case, _seeded = _seed_case_with_root("rb_unrelated")
    own = _add_unmarked_root(case, "east-region network partition dropped traffic")
    _confirm_root(case, own)
    assert confirmed_cause_interventions(case) == []


def test_confirmed_cause_interventions_via_cluster_duplicate():
    """Cluster collapse (same as confirmed_root_seed_origin): a self-discovered
    confirmed root that mirrors a seeded candidate still resolves to the seed's
    captured interventions."""
    stmt = "database connection pool exhausted under load"
    case, _seeded = _seed_case_with_root("rb_dbpool", root_stmt=stmt)
    dup = _add_unmarked_root(case, stmt)
    _confirm_root(case, dup)
    assert confirmed_cause_interventions(case) == [
        {"quadrant": "remediation", "ref": "root", "text": "fix the root"}
    ]


def test_candidate_solutions_block_renders_only_when_confirmed():
    """End-to-end through the real render helper: the <candidate_solutions> block
    surfaces the confirmed seeded cause's interventions (quadrant + text) so the
    LLM proposes them via solutions_to_add — and is EMPTY while the seed is still
    a candidate (the block fires only once the cause is established)."""
    from faultmaven.core.investigation.prompts.context_builder import (
        _build_candidate_solutions_block,
    )

    case, root_id = _seed_case_with_root("rb_render")
    # Candidate-only: nothing to offer as a fix yet.
    assert _build_candidate_solutions_block(case) == ""

    _confirm_root(case, root_id)
    block = _build_candidate_solutions_block(case)
    assert "<candidate_solutions>" in block
    assert "[remediation] fix the root" in block
    # It instructs the LLM to carry the quadrant through on the emission.
    assert "quadrant" in block


def test_candidate_solutions_block_empty_off_investigating():
    """Only INVESTIGATING renders the block — a terminal case has its own
    surface (and confirmed_cause_interventions is inert there anyway)."""
    from faultmaven.core.investigation.prompts.context_builder import (
        _build_candidate_solutions_block,
    )

    case, root_id = _seed_case_with_root("rb_render")
    _confirm_root(case, root_id)
    # Bypass the RESOLVED cross-field validator (needs resolved_at/closed_at) —
    # we only need the state field flipped to exercise the non-INVESTIGATING guard.
    object.__setattr__(case, "state", CaseState.RESOLVED)
    assert _build_candidate_solutions_block(case) == ""


# ---------------------------------------------------------------------------
# R8: rung_indicators -> seeded evidence-needs
# ---------------------------------------------------------------------------


def test_seeded_rung_indicator_becomes_pending_causal_need():
    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_cause("A")])], current_turn=3
    )
    assert report.seeded_anything
    hyp_id = report.seeded_hypothesis_ids[0]

    # The cause's one rung indicator became exactly one need.
    assert len(case.evidence_needs) == 1
    need = case.evidence_needs[0]
    assert report.seeded_need_ids == [need.need_id]

    # Prior-not-gate shape: PENDING, causal, LOW, fail-safe obtainability.
    assert need.purpose == NeedPurpose.CAUSAL_VERIFICATION
    assert need.state == NeedState.PENDING
    assert need.priority == NeedPriority.LOW
    assert need.obtainability == NeedObtainability.UNKNOWN
    assert need.is_outstanding
    # Motivated solely by the seeded hypothesis — the auto-supersession hook.
    assert need.motivating_hypothesis_ids == [hyp_id]
    assert need.created_at_turn == 3
    # The runbook step-reference prefix is stripped from the user-facing ask.
    assert need.request_text == "indicator for A"
    assert not need.request_text.startswith("[Step")
    # Origin lives only in the rationale (the read surface the blindness test bans
    # from safety modules) — never in a field a mechanism inspects.
    assert need.rationale.startswith(SEEDED_RATIONALE_PREFIX)
    assert "rb1" in need.rationale


def test_seeded_needs_are_never_auto_fulfilled():
    case = _case()
    seed_candidate_causes(case, [_runbook("rb1", [_cause("A")])], current_turn=1)
    assert case.evidence_needs  # at least one seeded need
    for need in case.evidence_needs:
        # A seeded need grounds only when a real datum arrives — none is linked.
        assert need.state == NeedState.PENDING
        assert need.fulfilling_evidence_ids == []


def test_seeded_needs_supersede_when_their_hypothesis_retires():
    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_cause("A")])], current_turn=1
    )
    hyp_id = report.seeded_hypothesis_ids[0]
    seeded = [n for n in case.evidence_needs if hyp_id in n.motivating_hypothesis_ids]
    assert seeded

    # The inherited motivator-based supersession (evidence-needs-design §7.4).
    flipped = _supersede_needs_on_hypothesis_retirement(case, hyp_id, current_turn=2)

    assert flipped == len(seeded)
    for need in seeded:
        assert need.state == NeedState.SUPERSEDED
        assert need.superseded_reason  # required for a SUPERSEDED need
        assert not need.is_outstanding


def test_seeded_needs_gate_the_wall_only_once_declared_unobtainable():
    # The soundness pair for R8's declared-data-wall interaction: a seeded
    # candidate's UNKNOWN-obtainability needs never wall it on their own (fail-
    # safe), but make the wall honestly computable once the model declares them
    # ungettable — pinned directly rather than by decomposition.
    from faultmaven.core.investigation.verification_status import (
        _candidate_unresolvable,
        _declared_wall,
    )

    case = _case()
    report = seed_candidate_causes(
        case, [_runbook("rb1", [_cause("A")])], current_turn=1
    )
    hyp_id = report.seeded_hypothesis_ids[0]
    seeded_needs = [
        n for n in case.evidence_needs if hyp_id in n.motivating_hypothesis_ids
    ]
    assert seeded_needs

    # Fail-safe: UNKNOWN discriminators never wall the candidate on their own.
    assert not _candidate_unresolvable(case, hyp_id)
    assert not _declared_wall(case)

    # Honestly computable: once every seeded rung is declared ungettable, the
    # candidate is unresolvable and (being the sole residual) the wall fires.
    for need in seeded_needs:
        need.obtainability = NeedObtainability.UNOBTAINABLE
    assert _candidate_unresolvable(case, hyp_id)
    assert _declared_wall(case)


def test_multiple_rung_indicators_each_emit_a_distinct_need():
    cause = _cause("A")
    cause["rung_indicators"] = {
        "root": ["[Step 1] root observable"],
        "s1": ["[Step 2] effect observable"],
    }
    case = _case()
    seed_candidate_causes(case, [_runbook("rb1", [cause])], current_turn=1)

    texts = sorted(n.request_text for n in case.evidence_needs)
    assert texts == ["effect observable", "root observable"]


def test_duplicate_rung_observable_seeds_one_need():
    cause = _cause("A")
    # Two rungs naming the identical observable — one need, not two.
    cause["rung_indicators"] = {
        "root": ["[Step 1] the same observable"],
        "s1": ["[Step 4] the same observable"],
    }
    case = _case()
    seed_candidate_causes(case, [_runbook("rb1", [cause])], current_turn=1)

    matching = [
        n for n in case.evidence_needs if n.request_text == "the same observable"
    ]
    assert len(matching) == 1


def test_cause_without_rung_indicators_emits_no_need():
    cause = _cause("A")
    cause["rung_indicators"] = {}
    case = _case()
    report = seed_candidate_causes(case, [_runbook("rb1", [cause])], current_turn=1)
    assert report.seeded_anything  # the chain still seeds
    assert case.evidence_needs == []
    assert report.seeded_need_ids == []


def test_emit_rung_needs_skips_indicators_empty_after_stripping():
    # An indicator that is only a step reference carries no observable content.
    case = _case()
    ids = _emit_rung_needs(
        case,
        "rb1",
        {"cause_letter": "A", "rung_indicators": {"root": ["[Step 1]", "  "]}},
        "hyp_x",
        current_turn=1,
    )
    assert ids == []
    assert case.evidence_needs == []


def test_emit_rung_needs_skips_non_list_indicator_values():
    # metadata["causes"] is read verbatim and may be malformed on the produce
    # path. A scalar value must not raise (the "never raised" contract); a bare
    # string must not enumerate per-character into garbage single-char needs.
    case = _case()
    ids = _emit_rung_needs(
        case,
        "rb1",
        {"cause_letter": "A", "rung_indicators": {"root": 5, "s1": "cpu"}},
        "hyp_x",
        current_turn=1,
    )
    assert ids == []
    assert case.evidence_needs == []


def test_malformed_rung_value_does_not_abort_or_orphan_the_seeded_cause():
    # A non-list rung value on an otherwise well-formed cause must leave the
    # candidate cleanly seeded and reported — never a hidden orphan hypothesis
    # excluded from the report (the seeder's try/except would mark it skipped).
    cause = _cause("A")
    cause["rung_indicators"] = {"root": ["[Step 1] good observable"], "s1": 7}
    case = _case()
    report = seed_candidate_causes(case, [_runbook("rb1", [cause])], current_turn=1)

    assert report.seeded_anything
    assert len(case.hypotheses) == 1  # not an orphan hidden from the report
    assert report.runbooks_contributing_nothing() == []  # no false alarm
    # The one well-formed rung still seeded its need; the malformed rung was
    # skipped, and the report accounting matches the case.
    texts = [n.request_text for n in case.evidence_needs]
    assert texts == ["good observable"]
    assert report.seeded_need_ids == [case.evidence_needs[0].need_id]


# ---------------------------------------------------------------------------
# GUARANTEE: provenance-blindness invariant
# ---------------------------------------------------------------------------


def test_safety_mechanisms_are_provenance_blind():
    """No safety mechanism (confidence decay, anchoring detection, failed-fix
    demotion, node/hypothesis state derivation, cause_state derivation, and the
    terminal/produce-side conclusion gates) may branch on seeded provenance — that
    is what keeps a seed mechanically indistinguishable from a self-generated
    hypothesis.

    THREE provenance surfaces exist, and ALL are checked: the node-metadata origin
    key (SEEDED_FROM_RUNBOOK_KEY), the hypothesis rationale text
    (SEEDED_RATIONALE_PREFIX), and the R9 captured-interventions key
    (SEEDED_INTERVENTIONS_KEY — present on a node only if it was seeded). No literal
    may appear in any safety module — a mechanism must not sniff origin out of the
    rationale string or the interventions surface either.

    The grep also bans the provenance SYMBOL NAMES themselves
    (``SEEDED_FROM_RUNBOOK_KEY``/``SEEDED_RATIONALE_PREFIX``/``SEEDED_INTERVENTIONS_KEY``)
    and the case-level origin helpers ``case_has_seeded_candidates``,
    ``confirmed_root_seed_origin``, and ``confirmed_cause_interventions``: a module
    could import the symbol and branch on origin without the literal *value* ever
    appearing in its source, so the literal-value grep alone is only a tripwire, not
    a proof. Banning the names closes that gap.
    (``seed_candidate_causes`` — the write path — is NOT banned: milestone_engine
    legitimately imports it to *create* seeds, which is not a safety mechanism
    reading origin.)

    The two R9 readers are NOT carved out here because no safety module reads them:
    ``confirmed_cause_interventions`` is read only by the prompt-render path
    (``context_builder._build_candidate_solutions_block``), which is not a safety
    mechanism (it offers a prior to the LLM, gated by M5 + user accept/verify) and
    is not in this module set — so the bare ban with no carve-out is exactly right.

    EXPLICIT CARVE-OUT (Phase 5.2b provenance-based uniqueness): exactly one
    origin reader — ``confirmed_root_seed_origin`` — is permitted in exactly one
    module — ``milestone_engine`` — and nowhere else. It backs the
    runbook-generation OFFER gate, which is a knowledge-lifecycle decision, NOT a
    safety mechanism: a wrong answer at that gate can only produce a missing or
    redundant "generate runbook" affordance, never an incorrect conclusion or a
    collapse under pressure (the manual create path and the async EXISTING_COVERS
    similarity backstop both remain). Every VALIDATION / decay / anchoring /
    demotion / state / gating path in this file's module set — including all the
    OTHER provenance surfaces in milestone_engine itself — stays blind. The
    carve-out is deliberately as narrow as one symbol in one module so it cannot
    become a general escape hatch.

    The module set spans consume-side safety (decay / anchoring / demotion /
    node+hypothesis state derivation in causal_graph + hypothesis_manager;
    cause_state derivation + the per-turn housekeeping loop in milestone_engine)
    AND the conclusion/terminal gates a seeded prior must never shortcut
    (cause_assurance, terminal_transitions, progress_monitor, state_validator,
    working_conclusion_generator). R8 adds the need-consuming safety paths:
    ``verification_status`` (the declared-data-wall arm that can move a case
    toward INSUFFICIENT_EVIDENCE) and ``evidence_need_surfacing`` (the render-time
    view) — because a seeded cause now emits evidence-needs, and those paths read
    needs, they must be proven not to reach through a need's motivating hypothesis
    to sniff seed origin.

    INVARIANT MAINTENANCE: this module set must track any move of safety logic.
    If decay/anchoring/demotion/state-derivation/gating is relocated to another
    module, add that module here — a whole-file grep is deliberately coarse so the
    guard can never be silently narrowed below where the safety logic actually
    lives.
    """
    import faultmaven.core.investigation.causal_graph as cg
    import faultmaven.core.investigation.cause_assurance as cause_assurance
    import faultmaven.core.investigation.evidence_need_surfacing as need_surfacing
    import faultmaven.core.investigation.hypothesis_manager as hmmod
    import faultmaven.core.investigation.milestone_engine as engine
    import faultmaven.core.investigation.progress_monitor as progress_monitor
    import faultmaven.core.investigation.state_validator as state_validator
    import faultmaven.core.investigation.terminal_transitions as terminal_transitions
    import faultmaven.core.investigation.verification_status as verification_status
    import faultmaven.core.investigation.working_conclusion_generator as wcg

    for module in (
        cg,
        hmmod,
        engine,
        cause_assurance,
        terminal_transitions,
        progress_monitor,
        state_validator,
        wcg,
        # R8 need-consuming safety paths: a seeded cause now emits
        # evidence-needs, so the declared-data-wall computation
        # (verification_status) and the surfacing view (evidence_need_surfacing)
        # must be proven blind to seed origin too.
        verification_status,
        need_surfacing,
    ):
        with open(module.__file__, "r", encoding="utf-8") as fh:
            source = fh.read()
        # (a) the literal provenance VALUES may not appear inline, and
        # (b) the provenance SYMBOL NAMES / case-level origin helpers may not be
        #     imported or referenced — a symbol import branches on origin with no
        #     literal value in the source.
        banned = (
            SEEDED_FROM_RUNBOOK_KEY,
            SEEDED_RATIONALE_PREFIX,
            SEEDED_INTERVENTIONS_KEY,
            "SEEDED_FROM_RUNBOOK_KEY",
            "SEEDED_RATIONALE_PREFIX",
            "SEEDED_INTERVENTIONS_KEY",
            "case_has_seeded_candidates",
            "confirmed_root_seed_origin",
            "confirmed_cause_interventions",
        )
        for marker in banned:
            # Documented carve-out: the offer-gate origin reader is permitted in
            # exactly the offer module (see docstring). Nothing else is.
            if marker == "confirmed_root_seed_origin" and module is engine:
                continue
            assert marker not in source, (
                f"{module.__name__} references seed provenance ({marker!r}) — a "
                "safety mechanism must never branch on origin"
            )
