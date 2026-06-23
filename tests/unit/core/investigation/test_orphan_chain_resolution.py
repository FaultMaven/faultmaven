"""Unit tests for orphan-chain resolution (step 3: T1 deterministic re-attach +
T2a ambiguous LLM-nudge).

The divergence under test: the LLM emits a real root->D chain but leaves it
unlinked, so the hypothesis runs its lifecycle on a degenerate bridge stub while
a parallel orphan chain describes the SAME cause (double-representation). The
deterministic post-pass ``resolve_orphan_chains`` re-attaches an UNAMBIGUOUS
double-representation in place (T1) and returns the ambiguous remainder for a
one-turn LLM nudge (T2a). The B2c invariant it serves: every chain explaining D
is attached to exactly one hypothesis.
"""

import pytest

from faultmaven.core.investigation.causal_graph import (
    RESTATEMENT_AMBIGUOUS,
    RESTATEMENT_STRONG,
    resolve_orphan_chains,
    restatement_score,
    seed_problem_node,
)
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    InquiryData,
    NodeType,
    ProblemVerification,
)
from tests.utils import bridge_flat_hypotheses_to_graph

pytestmark = pytest.mark.unit


def _investigating_case() -> Case:
    return Case(
        case_id=f"case_{'0' * 12}",
        user_id="user_alpha",
        organization_id="org_alpha",
        title="Checkout 5xx",
        description="Checkout service returns 500s under load",
        state=CaseState.INVESTIGATING,
        current_turn=4,
        inquiry=InquiryData(
            proposed_problem_statement="Checkout returns 500s",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="Checkout returns 500s",
            severity=CaseSeverity.HIGH,
        ),
    )


def _hyp(statement: str) -> Hypothesis:
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.DATABASE,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=4,
        rationale="initial",
        evidence_links=[],
    )


def _problem_id(case: Case) -> str:
    return next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )


def _add_orphan_chain(case: Case, root_stmt: str, *, multi_rung: bool = True) -> str:
    """Add an unlinked chain root(->interm)->D and return the root node id."""
    d_id = _problem_id(case)
    root = CausalNode(statement=root_stmt, node_type=NodeType.ROOT, generated_at_turn=4)
    case.causal_nodes[root.node_id] = root
    if multi_rung:
        interm = CausalNode(
            statement="connections are acquired but never released",
            node_type=NodeType.INTERMEDIATE,
            generated_at_turn=4,
        )
        case.causal_nodes[interm.node_id] = interm
        case.causal_edges.append(
            CausalEdge(
                cause_node_id=root.node_id,
                effect_node_id=interm.node_id,
                created_at_turn=4,
            )
        )
        case.causal_edges.append(
            CausalEdge(
                cause_node_id=interm.node_id, effect_node_id=d_id, created_at_turn=4
            )
        )
    else:
        case.causal_edges.append(
            CausalEdge(
                cause_node_id=root.node_id, effect_node_id=d_id, created_at_turn=4
            )
        )
    return root.node_id


def _orphan_roots(case: Case) -> list[str]:
    referenced = set()
    for h in case.hypotheses.values():
        if h.root_node_id:
            referenced.add(h.root_node_id)
        referenced.update(h.path or [])
    return [
        nid
        for nid, n in case.causal_nodes.items()
        if n.node_type == NodeType.ROOT and nid not in referenced
    ]


# --- scoring sanity --------------------------------------------------------


def test_restatement_score_thresholds_bracket_expectations():
    # Strong restatement (heavy token containment).
    assert (
        restatement_score(
            "leaked database connection exhausts the pool",
            "connection pool exhausted by a leaked database connection",
        )
        >= RESTATEMENT_STRONG
    )
    # Unrelated causes do not match.
    assert (
        restatement_score("tls certificate expired", "redis eviction storm")
        < RESTATEMENT_AMBIGUOUS
    )


def test_stemming_matches_morphological_variants():
    """Gate 2: inflectional variants of the same words now match (the leaks≠leak /
    connections≠connection brittleness that left a validated chain orphaned)."""
    from faultmaven.core.investigation.causal_graph import _stem

    assert _stem("leaks") == _stem("leaking") == _stem("leak") == "leak"
    assert _stem("connections") == _stem("connection") == "connection"
    assert _stem("exhausted") == _stem("exhausting") == _stem("exhaust") == "exhaust"
    # Silent-e plurals keep their 'e' (single -s strip, not -es).
    assert _stem("caches") == _stem("cache") == "cache"
    assert _stem("nodes") == _stem("node") == "node"
    # Conservative: -ss/-us/-is are not plural markers; short / id tokens untouched.
    assert _stem("process") == "process"
    assert _stem("status") == "status"
    assert _stem("basis") == "basis"
    assert _stem("pod") == "pod"
    assert _stem("cn_0a1b2c3d4e5f") == "cn_0a1b2c3d4e5f"  # non-alpha untouched
    # NO over-merge of unrelated words: the silent-e / short-verb collisions a
    # crude stemmer would create (codes/cods->cod, caring->car) must NOT happen.
    assert _stem("codes") == "code" and _stem("cods") == "cod"  # distinct stems
    assert _stem("caring") == "caring" and _stem("cars") == "car"  # caring is NOT car
    assert _stem("rates") == "rate" and _stem("rats") == "rat"  # distinct stems

    # A morphologically-divergent restatement that scored too low before stemming
    # now clears STRONG.
    assert (
        restatement_score(
            "code change leaks connections on the error path",
            "a connection leak in the deployed code",
        )
        >= RESTATEMENT_STRONG
    )
    # Stemming does not manufacture a match between unrelated statements.
    assert (
        restatement_score("dns resolution timeout", "memory pressure evictions")
        < RESTATEMENT_AMBIGUOUS
    )


# --- T1: deterministic re-attach -------------------------------------------


def test_t1_reattaches_morphological_orphan_over_a_distractor():
    """The disp-confirmed behavioral case: a VALIDATED chain root the LLM left
    orphaned (morphological variants of a leak hypothesis) is now a clean single
    match (stemming) over an unrelated distractor hypothesis → T1 auto-attaches,
    so a validated root no longer fails to ground cause_state."""
    leak = _hyp("a connection leak in the deployed checkout code")
    distractor = _hyp("the number of replicas increased beyond the pool size")
    case = _investigating_case()
    case.hypotheses = {leak.hypothesis_id: leak, distractor.hypothesis_id: distractor}
    bridge_flat_hypotheses_to_graph(case)  # each gets a degenerate stub

    orphan_root = _add_orphan_chain(
        case,
        "a code change in PlaceOrder removed the release call, leaking connections",
        multi_rung=True,
    )
    ambiguous = resolve_orphan_chains(case)

    assert ambiguous == []  # resolved deterministically, no nudge
    assert leak.root_node_id == orphan_root  # the leak hyp owns the real chain
    assert distractor.root_node_id != orphan_root  # the distractor is untouched


def test_t1_reattaches_unambiguous_double_representation_and_gcs_stub():
    hyp = _hyp("connection pool exhausted by a leaked database connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)  # hyp gets a degenerate stub
    stub_root = hyp.root_node_id
    assert hyp.path == [stub_root, _problem_id(case)]  # degenerate (len 2)

    orphan_root = _add_orphan_chain(
        case, "a leaked database connection exhausts the pool", multi_rung=True
    )

    ambiguous = resolve_orphan_chains(case)

    # T1 handled it deterministically — nothing left for the LLM nudge.
    assert ambiguous == []
    # The hypothesis now owns the real multi-rung chain.
    assert hyp.root_node_id == orphan_root
    assert hyp.path[0] == orphan_root
    assert hyp.path[-1] == _problem_id(case)
    assert len(hyp.path) == 3  # root -> intermediate -> D
    # The abandoned bridge stub is GC'd; no orphan roots remain.
    assert stub_root not in case.causal_nodes
    assert _orphan_roots(case) == []


def test_t1_reattaches_a_flat_hypothesis_onto_orphan_chain():
    # Post-B2c (no bridge), a hypothesis the LLM emits a chain for but leaves
    # unlinked is FLAT (no stub), not bridge-projected. T1 must still re-attach
    # the flat hypothesis onto the orphan chain — there is simply no old stub to
    # GC. This is the real production scenario after the bridge removal.
    hyp = _hyp("connection pool exhausted by a leaked database connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    seed_problem_node(case)  # only D is engine-seeded; the hypothesis stays flat
    assert hyp.root_node_id is None

    orphan_root = _add_orphan_chain(
        case, "a leaked database connection exhausts the pool", multi_rung=True
    )

    ambiguous = resolve_orphan_chains(case)

    assert ambiguous == []
    assert hyp.root_node_id == orphan_root
    assert len(hyp.path) == 3  # root -> intermediate -> D
    assert _orphan_roots(case) == []


def test_t1_dedupes_two_degenerate_roots_for_one_cause():
    # Orphan root is itself degenerate (root->D); re-attaching still dedupes the
    # bridge stub against the emitted root for the same cause.
    hyp = _hyp("connection pool exhausted by a leaked database connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    stub_root = hyp.root_node_id

    orphan_root = _add_orphan_chain(
        case, "a leaked database connection exhausts the pool", multi_rung=False
    )

    assert resolve_orphan_chains(case) == []
    assert hyp.root_node_id == orphan_root
    assert stub_root not in case.causal_nodes
    assert _orphan_roots(case) == []


def test_t1_requires_substantive_overlap_not_single_token_containment():
    # A 1-token orphan fully contained in the hypothesis scores containment=1.0
    # (STRONG), but a single shared word is too flimsy to auto-attach — it must
    # fall through to a nudge, not deterministically re-root.
    hyp = _hyp("a database deadlock blocks the writer thread")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    stub_root = hyp.root_node_id

    orphan_root = _add_orphan_chain(case, "deadlock", multi_rung=True)
    # Confirm the metric WOULD call it STRONG on the lone shared token...
    assert restatement_score("deadlock", hyp.statement) >= RESTATEMENT_STRONG

    ambiguous = resolve_orphan_chains(case)

    # ...but the substantive-overlap guard blocks the auto-attach.
    assert hyp.root_node_id == stub_root  # not re-rooted
    assert [o["root_id"] for o in ambiguous] == [orphan_root]


def test_t1_does_not_reattach_same_hypothesis_to_two_orphans():
    # Two degenerate orphan roots both restate one hypothesis. The hypothesis is
    # adopted by the first; the second must NOT re-root it again (no churn) and
    # is left as a standalone candidate.
    hyp = _hyp("connection pool exhausted by a leaked database connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)

    o1 = _add_orphan_chain(
        case, "a leaked database connection exhausts the pool", multi_rung=False
    )
    o2 = _add_orphan_chain(
        case, "the pool drains from a leaked database connection", multi_rung=False
    )

    ambiguous = resolve_orphan_chains(case)

    assert ambiguous == []
    # Exactly one orphan became the hypothesis's root; the other is left standalone.
    assert hyp.root_node_id in (o1, o2)
    leftover = o2 if hyp.root_node_id == o1 else o1
    assert leftover in case.causal_nodes
    assert leftover in _orphan_roots(case)


def test_adopted_hypothesis_does_not_block_a_clean_second_match():
    # orphan1 cleanly restates H; orphan2 cleanly restates G but ALSO overlaps H
    # weakly. Excluding the already-adopted H from orphan2's scoring keeps G's
    # match unambiguous, so BOTH re-attach (no spurious downgrade to a nudge).
    h = _hyp("database connection pool exhausted")
    g = _hyp("redis cache eviction storm overload")
    case = _investigating_case()
    case.hypotheses = {h.hypothesis_id: h, g.hypothesis_id: g}
    bridge_flat_hypotheses_to_graph(case)

    o1 = _add_orphan_chain(
        case, "database connection pool exhausted by a leak", multi_rung=True
    )
    o2 = _add_orphan_chain(
        case,
        "redis cache eviction storm from connection pool pressure",
        multi_rung=True,
    )
    # o2 genuinely overlaps H (shares connection+pool) at/above the ambiguity floor.
    assert restatement_score(case.causal_nodes[o2].statement, h.statement) >= (
        RESTATEMENT_AMBIGUOUS
    )

    ambiguous = resolve_orphan_chains(case)

    assert ambiguous == []  # neither downgraded to a nudge
    assert h.root_node_id == o1
    assert g.root_node_id == o2


# --- T2a: ambiguous -> nudge, no auto-attach -------------------------------


def test_t2a_returns_ambiguous_orphan_matching_two_hypotheses():
    h1 = _hyp("database connection pool exhausted")
    h2 = _hyp("database connection leak in checkout")
    case = _investigating_case()
    case.hypotheses = {h1.hypothesis_id: h1, h2.hypothesis_id: h2}
    bridge_flat_hypotheses_to_graph(case)
    h1_root, h2_root = h1.root_node_id, h2.root_node_id

    orphan_root = _add_orphan_chain(
        case, "database connection pool exhausted by a leak", multi_rung=True
    )

    ambiguous = resolve_orphan_chains(case)

    assert len(ambiguous) == 1
    assert ambiguous[0]["root_id"] == orphan_root
    assert len(ambiguous[0]["candidate_hypotheses"]) == 2
    # No re-attach happened — both hypotheses keep their stubs.
    assert h1.root_node_id == h1_root
    assert h2.root_node_id == h2_root


def test_t2a_returns_ambiguous_orphan_on_weak_match():
    hyp = _hyp("redis cache eviction storm overload")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    stub_root = hyp.root_node_id

    # Shares exactly two content tokens -> score lands in [AMBIGUOUS, STRONG).
    orphan_root = _add_orphan_chain(
        case, "cache eviction disk pressure node", multi_rung=True
    )
    score = restatement_score("cache eviction disk pressure node", hyp.statement)
    assert RESTATEMENT_AMBIGUOUS <= score < RESTATEMENT_STRONG

    ambiguous = resolve_orphan_chains(case)

    assert [o["root_id"] for o in ambiguous] == [orphan_root]
    assert hyp.root_node_id == stub_root  # not re-attached


def test_t2a_does_not_clobber_a_hypothesis_already_on_a_real_chain():
    # The hypothesis already owns a real multi-rung chain; an orphan that
    # restates it is a separate representation, surfaced for a nudge — never an
    # auto re-root that would destroy the existing chain.
    from faultmaven.core.investigation.causal_graph import (
        chain_path_to_problem,
        seed_problem_node,
    )

    hyp = _hyp("connection pool exhausted by a leaked database connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    seed_problem_node(case)  # anchor D without stubbing the hypothesis
    real_root = _add_orphan_chain(case, hyp.statement, multi_rung=True)
    # Manually link the hypothesis to that real chain (its own, not an orphan).
    hyp.root_node_id = real_root
    hyp.path = chain_path_to_problem(real_root, case)
    assert len(hyp.path) == 3
    original_path = list(hyp.path)

    orphan_root = _add_orphan_chain(
        case, "a leaked database connection exhausts the pool", multi_rung=True
    )

    ambiguous = resolve_orphan_chains(case)

    assert [o["root_id"] for o in ambiguous] == [orphan_root]
    assert hyp.path == original_path  # untouched


# --- benign: standalone candidate root -------------------------------------


def test_benign_orphan_root_is_left_as_standalone_candidate():
    hyp = _hyp("redis cache eviction storm overload")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)

    orphan_root = _add_orphan_chain(
        case, "tls certificate expired on the gateway", multi_rung=True
    )

    ambiguous = resolve_orphan_chains(case)

    assert ambiguous == []  # not surfaced as a divergence
    assert orphan_root in case.causal_nodes  # left in place, not dropped
    assert orphan_root in _orphan_roots(case)  # still a standalone candidate


def test_open_orphan_chain_not_yet_reaching_d_is_left_alone():
    hyp = _hyp("connection pool exhausted by a leaked database connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    stub_root = hyp.root_node_id

    # Orphan root with no edge to D (open chain).
    root = CausalNode(
        statement="a leaked database connection exhausts the pool",
        node_type=NodeType.ROOT,
        generated_at_turn=4,
    )
    case.causal_nodes[root.node_id] = root

    assert resolve_orphan_chains(case) == []
    assert hyp.root_node_id == stub_root  # untouched (chain not anchored)
    assert root.node_id in case.causal_nodes


# --- engine wiring: T2a -> system_feedback ---------------------------------


def test_engine_nudge_appends_system_feedback_for_ambiguous_orphan():
    h1 = _hyp("database connection pool exhausted")
    h2 = _hyp("database connection leak in checkout")
    case = _investigating_case()
    case.hypotheses = {h1.hypothesis_id: h1, h2.hypothesis_id: h2}
    bridge_flat_hypotheses_to_graph(case)
    _add_orphan_chain(
        case, "database connection pool exhausted by a leak", multi_rung=True
    )

    metadata: dict = {}
    MilestoneEngine._nudge_ambiguous_orphan_chains(case, metadata)

    fb = metadata.get("system_feedback", "")
    assert "Unlinked causal chain" in fb
    assert "re-root" in fb


def test_engine_nudge_silent_and_reattaches_on_unambiguous_case():
    hyp = _hyp("connection pool exhausted by a leaked database connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    orphan_root = _add_orphan_chain(
        case, "a leaked database connection exhausts the pool", multi_rung=True
    )

    metadata: dict = {}
    MilestoneEngine._nudge_ambiguous_orphan_chains(case, metadata)

    # T1 re-attached; no nudge emitted.
    assert "system_feedback" not in metadata or not metadata["system_feedback"]
    assert hyp.root_node_id == orphan_root
