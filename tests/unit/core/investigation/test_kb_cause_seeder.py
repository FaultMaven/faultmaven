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
    SEEDED_RATIONALE_PREFIX,
    SeededRunbook,
    SkipClass,
    confirmed_root_seed_origin,
    seed_candidate_causes,
)
from faultmaven.modules.case.contracts import (
    Case,
    CausalNode,
    CaseSeverity,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
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


def test_none_on_empty_graph():
    case = _case()
    assert confirmed_root_seed_origin(case) is None


# ---------------------------------------------------------------------------
# GUARANTEE: provenance-blindness invariant
# ---------------------------------------------------------------------------


def test_safety_mechanisms_are_provenance_blind():
    """No safety mechanism (confidence decay, anchoring detection, failed-fix
    demotion, node/hypothesis state derivation, cause_state derivation, and the
    terminal/produce-side conclusion gates) may branch on seeded provenance — that
    is what keeps a seed mechanically indistinguishable from a self-generated
    hypothesis.

    Two provenance surfaces exist, and BOTH are checked: the node-metadata key
    (SEEDED_FROM_RUNBOOK_KEY) and the hypothesis rationale text
    (SEEDED_RATIONALE_PREFIX). Neither literal may appear in any safety module —
    a mechanism must not sniff origin out of the rationale string either.

    The grep also bans the provenance SYMBOL NAMES themselves
    (``SEEDED_FROM_RUNBOOK_KEY``/``SEEDED_RATIONALE_PREFIX``) and the case-level
    origin helpers ``case_has_seeded_candidates`` and ``confirmed_root_seed_origin``:
    a module could import the symbol and branch on origin without the literal
    *value* ever appearing in its source, so the literal-value grep alone is only
    a tripwire, not a proof. Banning the names closes that gap.
    (``seed_candidate_causes`` — the write path — is NOT banned: milestone_engine
    legitimately imports it to *create* seeds, which is not a safety mechanism
    reading origin.)

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
    working_conclusion_generator).

    INVARIANT MAINTENANCE: this module set must track any move of safety logic.
    If decay/anchoring/demotion/state-derivation/gating is relocated to another
    module, add that module here — a whole-file grep is deliberately coarse so the
    guard can never be silently narrowed below where the safety logic actually
    lives.
    """
    import faultmaven.core.investigation.causal_graph as cg
    import faultmaven.core.investigation.cause_assurance as cause_assurance
    import faultmaven.core.investigation.hypothesis_manager as hmmod
    import faultmaven.core.investigation.milestone_engine as engine
    import faultmaven.core.investigation.progress_monitor as progress_monitor
    import faultmaven.core.investigation.state_validator as state_validator
    import faultmaven.core.investigation.terminal_transitions as terminal_transitions
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
            "SEEDED_FROM_RUNBOOK_KEY",
            "SEEDED_RATIONALE_PREFIX",
            "case_has_seeded_candidates",
            "confirmed_root_seed_origin",
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
