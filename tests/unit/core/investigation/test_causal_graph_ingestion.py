"""Unit tests for the chain-emission ingestion primitives (PR B2 slice a).

``ingest_emitted_chain`` builds the causal graph from a turn's LLM-emitted
chain fragments (lazy backward expansion, methodology §5/S3); ``seed_problem_node``
anchors ``D``; ``chain_path_to_problem`` derives a root→D path. These are pure
(no engine wiring yet — the prompt + flag that drive them land in a later slice).
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import (
    chain_path_to_problem,
    ingest_emitted_chain,
    seed_problem_node,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    EvidenceStance,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


def _case(*, with_problem=True) -> Case:
    kwargs = dict(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Deploy fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    if with_problem:
        kwargs["problem_verification"] = ProblemVerification(
            symptom_statement="Deploy to on-prem job fails",
            severity=CaseSeverity.HIGH,
        )
    case = Case(**kwargs)
    case.current_turn = 4
    return case


def _node(statement, node_type, produces=None, and_group=None):
    return SimpleNamespace(
        statement=statement, node_type=node_type, produces=produces, and_group=and_group
    )


def test_ingest_skips_empty_statement_node_without_raising():
    # CausalNode rejects an empty statement; ingestion must skip (never raise)
    # and keep new_index_N indices aligned (None placeholder).
    case = _case()
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[
            _node("   ", NodeType.ROOT, produces="D"),  # whitespace -> skipped
            _node("real root", NodeType.ROOT, produces="D"),
        ],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    assert created[0] is None  # skipped, index preserved
    assert created[1] is not None
    # Only the real root + D exist; no edge from the skipped node.
    assert len(case.causal_nodes) == 2


def test_ingest_skips_emitted_problem_node():
    # D is engine-seeded; an emitted PROBLEM node would create a second D and
    # violate the one-PROBLEM-per-case index. It must be skipped.
    case = _case()
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("fake D", NodeType.PROBLEM, produces=None)],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    assert created == [None]
    problem_nodes = [
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    ]
    assert len(problem_nodes) == 1  # only the engine-seeded D


def test_chain_path_finds_d_despite_a_dead_end_branch_first():
    # Regression for the greedy single-walk bug: the dead-end branch is emitted
    # FIRST, but BFS still finds root -> D.
    case = _case()
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[
            _node("dead end", NodeType.INTERMEDIATE),  # no produces -> dangling
            _node("root", NodeType.ROOT, produces="new_index_0"),  # root -> dead end
        ],
        # ...and an explicit root -> D edge added AFTER the dead-end edge.
        edges_to_add=[
            SimpleNamespace(
                cause="new_index_1", effect="D", and_group=None, reasoning=None
            )
        ],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    dead_id, root_id = created
    d_id = next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    assert chain_path_to_problem(root_id, case) == [root_id, d_id]


def test_chain_path_empty_when_root_is_the_problem_node():
    case = _case()
    d = seed_problem_node(case)
    assert chain_path_to_problem(d.node_id, case) == []


def test_ingest_skips_node_evidence_missing_stance():
    case = _case()
    case.evidence.append(SimpleNamespace(evidence_id="ev_0123456789ab"))
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("root", NodeType.ROOT, produces="D")],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(  # no `stance` attribute -> skipped, no AttributeError
                node_ref="new_index_0",
                evidence_id="ev_0123456789ab",
                reasoning="r",
                stance_confidence=1.0,
            )
        ],
        current_turn=case.current_turn,
    )
    assert case.causal_nodes[created[0]].evidence_links == []


def test_seed_problem_node_is_idempotent():
    case = _case()
    d1 = seed_problem_node(case)
    d2 = seed_problem_node(case)
    assert d1 is not None and d1.node_id == d2.node_id
    assert d1.node_type == NodeType.PROBLEM
    assert [
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    ] == [d1]


def test_seed_noop_without_problem_statement():
    case = _case(with_problem=False)
    assert seed_problem_node(case) is None
    assert case.causal_nodes == {}


def test_ingest_builds_root_mid_d_chain_via_produces():
    case = _case()
    # Emission order: mid (produces D), root (produces the mid via new_index_0).
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[
            _node("connection refused at the pod", NodeType.INTERMEDIATE, produces="D"),
            _node(
                "NetworkPolicy denies ingress",
                NodeType.ROOT,
                produces="new_index_0",
            ),
        ],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )

    mid_id, root_id = created
    d_id = next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    # 3 nodes (D + mid + root), 2 edges (root->mid, mid->D).
    assert len(case.causal_nodes) == 3
    assert {(e.cause_node_id, e.effect_node_id) for e in case.causal_edges} == {
        (root_id, mid_id),
        (mid_id, d_id),
    }
    assert chain_path_to_problem(root_id, case) == [root_id, mid_id, d_id]


def test_ingest_explicit_edges_support_convergence():
    case = _case()
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[
            _node("root A", NodeType.ROOT, produces="D"),
            _node("root B", NodeType.ROOT),  # no produces; linked via explicit edge
        ],
        edges_to_add=[
            SimpleNamespace(
                cause="new_index_1",
                effect="D",
                and_group=None,
                reasoning="B also causes D",
            )
        ],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    a_id, b_id = created
    d_id = next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    assert (a_id, d_id) in {
        (e.cause_node_id, e.effect_node_id) for e in case.causal_edges
    }
    assert (b_id, d_id) in {
        (e.cause_node_id, e.effect_node_id) for e in case.causal_edges
    }


def test_ingest_attaches_node_evidence_and_skips_unknown():
    case = _case()
    case.evidence.append(SimpleNamespace(evidence_id="ev_0123456789ab"))
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("the root", NodeType.ROOT, produces="D")],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref="new_index_0",
                evidence_id="ev_0123456789ab",
                stance=EvidenceStance.SUPPORTS,
                reasoning="confirms",
                stance_confidence=0.9,
            ),
            SimpleNamespace(  # unknown evidence id -> skipped, no crash
                node_ref="new_index_0",
                evidence_id="ev_doesnotexist",
                stance=EvidenceStance.REFUTES,
                reasoning="x",
                stance_confidence=1.0,
            ),
        ],
        current_turn=case.current_turn,
    )
    root = case.causal_nodes[created[0]]
    assert len(root.evidence_links) == 1
    assert root.evidence_links[0].evidence_id == "ev_0123456789ab"
    assert root.evidence_links[0].stance == EvidenceStance.SUPPORTS


def test_ingest_resolves_new_index_evidence_ref():
    # Same-turn evidence: evidence_id_ref='new_index_N' resolves against the
    # evidence created earlier this turn (the common rung-evidence case).
    case = _case()
    case.evidence.append(SimpleNamespace(evidence_id="ev_aaaaaaaaaaaa"))
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("the root", NodeType.ROOT, produces="D")],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref="new_index_0",
                evidence_id_ref="new_index_0",  # same-turn evidence placeholder
                stance=EvidenceStance.SUPPORTS,
                reasoning="confirms",
                stance_confidence=0.9,
            )
        ],
        current_turn=case.current_turn,
        evidence_created_ids=["ev_aaaaaaaaaaaa"],
    )
    root = case.causal_nodes[created[0]]
    assert len(root.evidence_links) == 1
    assert root.evidence_links[0].evidence_id == "ev_aaaaaaaaaaaa"


def test_ingest_skips_unresolvable_refs_and_is_idempotent_on_edges():
    case = _case()
    ingest_emitted_chain(
        case,
        nodes_to_add=[
            _node("root", NodeType.ROOT, produces="new_index_9")
        ],  # out of range
        edges_to_add=[
            SimpleNamespace(
                cause="cn_nonexistent0", effect="D", and_group=None, reasoning=None
            )
        ],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    # The dangling produces + the dangling explicit edge both skipped: no edges.
    assert case.causal_edges == []


def test_ingest_noop_without_problem_statement():
    case = _case(with_problem=False)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("root", NodeType.ROOT, produces="D")],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    assert created == []
    assert case.causal_nodes == {}


def test_chain_path_returns_empty_for_open_chain():
    case = _case()
    # root with no produces -> no path to D.
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("orphan root", NodeType.ROOT)],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    assert chain_path_to_problem(created[0], case) == []


def test_ingested_nodes_default_to_candidate():
    case = _case()
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("root", NodeType.ROOT, produces="D")],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    assert case.causal_nodes[created[0]].node_state == NodeState.CANDIDATE


def test_ingest_accepts_real_emission_schema_objects():
    # The ingestion duck-types; this pins that the actual LLM-facing schema
    # objects (CausalNodeToAdd / CausalEdgeToAdd / NodeEvidenceLinkToAdd) feed it.
    from faultmaven.core.investigation.schemas import (
        CausalEdgeToAdd,
        CausalNodeToAdd,
        NodeEvidenceLinkToAdd,
    )

    case = _case()
    case.evidence.append(SimpleNamespace(evidence_id="ev_0123456789ab"))
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[
            CausalNodeToAdd(
                statement="connection refused",
                node_type=NodeType.INTERMEDIATE,
                produces="D",
            ),
            CausalNodeToAdd(
                statement="NetworkPolicy denies ingress",
                node_type=NodeType.ROOT,
                produces="new_index_0",
            ),
        ],
        edges_to_add=[CausalEdgeToAdd(cause="new_index_1", effect="new_index_0")],
        node_evidence=[
            NodeEvidenceLinkToAdd(
                node_ref="new_index_1",
                evidence_id_ref="ev_0123456789ab",
                stance=EvidenceStance.SUPPORTS,
                reasoning="confirms the policy",
            )
        ],
        current_turn=case.current_turn,
    )
    mid_id, root_id = created
    assert chain_path_to_problem(root_id, case) == [
        root_id,
        mid_id,
        next(
            n.node_id
            for n in case.causal_nodes.values()
            if n.node_type == NodeType.PROBLEM
        ),
    ]
    assert len(case.causal_nodes[root_id].evidence_links) == 1


def test_ingest_dedup_reuses_node_on_exact_restatement():
    # The LLM re-asserts a standing cause on a later turn. An exact restatement
    # (modulo whitespace/case) must REUSE the existing same-type node, not mint a
    # duplicate root — so one cause stays on one node and its grounding does not
    # fragment. Reuse covers the cross-turn re-emission seen on kafka-consumer-lag.
    case = _case()
    first = ingest_emitted_chain(
        case,
        nodes_to_add=[
            _node("Missing index on orders(customer_id)", NodeType.ROOT, produces="D")
        ],
        edges_to_add=[],
        node_evidence=[],
        current_turn=4,
    )
    root_id = first[0]
    n_before = len(case.causal_nodes)
    second = ingest_emitted_chain(
        case,
        nodes_to_add=[
            # same cause, different whitespace + case -> normalized exact match
            _node(
                "missing index  on   ORDERS(customer_id)", NodeType.ROOT, produces="D"
            )
        ],
        edges_to_add=[],
        node_evidence=[],
        current_turn=5,
    )
    assert second[0] == root_id  # reused the canonical node
    assert len(case.causal_nodes) == n_before  # no duplicate minted
    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    assert len(roots) == 1


def test_ingest_dedup_does_not_merge_distinct_siblings():
    # Two roots differing in one parameter are DISTINCT OR-siblings and must NOT
    # be merged — exact match only, never a fuzzy collapse (the over-merge trap).
    case = _case()
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[
            _node("Missing index on orders(customer_id)", NodeType.ROOT, produces="D"),
            _node("Missing index on orders(order_date)", NodeType.ROOT, produces="D"),
        ],
        edges_to_add=[],
        node_evidence=[],
        current_turn=4,
    )
    assert created[0] != created[1]
    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    assert len(roots) == 2


def test_ingest_strips_llm_supports_link_on_absence_evidence():
    """M2 trust boundary: counterfactual CONFIRMATION is engine-reserved. An
    LLM-emitted SUPPORTS link on a causal_absence row is stripped at ingest
    (it would mint the CONFIRMED grade — 'verified', the harvest bar, the
    grounded axis — from a self-claim); REFUTES-on-absence stays accepted
    (counterfactual disconfirmation feeds M6)."""
    from datetime import datetime, timezone

    from faultmaven.modules.case.contracts import (
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        EvidenceStance,
    )

    case = _case()
    d = seed_problem_node(case)
    absence = Evidence(
        evidence_id="ev_aaaaaaaaaaa1",
        summary="errors gone after restart",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )
    case.evidence.append(absence)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[
            SimpleNamespace(
                statement="kernel conntrack table saturation drops packets",
                node_type="root",
                produces="D",
                and_group=None,
            )
        ],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref="new_index_0",
                evidence_id="ev_aaaaaaaaaaa1",
                stance="supports",
                reasoning="it went away",
            ),
            SimpleNamespace(
                node_ref="new_index_0",
                evidence_id="ev_aaaaaaaaaaa1",
                stance="refutes",
                reasoning="fix applied, problem persists",
            ),
        ],
        current_turn=case.current_turn,
    )
    root = case.causal_nodes[created[0]]
    stances = [link.stance for link in root.evidence_links]
    assert EvidenceStance.SUPPORTS not in stances  # confirmation stripped
    assert EvidenceStance.REFUTES in stances  # disconfirmation accepted


def _causal_row(ev_id, summary):
    from datetime import datetime, timezone

    from faultmaven.modules.case.contracts import (
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
    )

    return Evidence(
        evidence_id=ev_id,
        summary=summary,
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def test_ingest_reemission_upserts_stance_confidence():
    """INV-29: a re-emitted (node, evidence) link is an UPSERT, matching the
    junction table's ON CONFLICT DO UPDATE — a raised stance_confidence after
    corroboration must land (first-write-wins would freeze a hedged link out
    of the §7.1 causal tally forever)."""
    from faultmaven.modules.case.contracts import EvidenceStance

    case = _case()
    seed_problem_node(case)
    case.evidence.append(_causal_row("ev_bbbbbbbbbbb1", "pool max_size dropped to 5"))
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("undersized connection pool", NodeType.ROOT, produces="D")],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref="new_index_0",
                evidence_id="ev_bbbbbbbbbbb1",
                stance="supports",
                reasoning="tentative",
                stance_confidence=0.5,
            )
        ],
        current_turn=case.current_turn,
    )
    root = case.causal_nodes[created[0]]
    assert root.evidence_links[0].stance_confidence == 0.5
    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref=created[0],
                evidence_id="ev_bbbbbbbbbbb1",
                stance="supports",
                reasoning="corroborated on the config diff",
                stance_confidence=0.9,
            )
        ],
        current_turn=case.current_turn + 1,
    )
    assert len(root.evidence_links) == 1  # upsert, not a duplicate
    assert root.evidence_links[0].stance_confidence == 0.9
    assert root.evidence_links[0].stance == EvidenceStance.SUPPORTS


def test_ingest_never_overwrites_absence_row_links():
    """Links on causal_absence rows are engine-verdict territory: an LLM
    REFUTES re-emission must not launder away a standing confirmation link
    (the strip already blocks SUPPORTS creation; this pins the overwrite
    side)."""
    from datetime import datetime, timezone

    from faultmaven.modules.case.contracts import (
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        EvidenceStance,
        NodeEvidenceLink,
    )

    case = _case()
    seed_problem_node(case)
    absence = Evidence(
        evidence_id="ev_ccccccccccc1",
        summary="problem gone after the fix",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="user",
        collected_at_turn=3,
        collected_at=datetime.now(timezone.utc),
    )
    case.evidence.append(absence)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[
            _node("stale kubeconfig credential", NodeType.ROOT, produces="D")
        ],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    root = case.causal_nodes[created[0]]
    # Engine confirmation link (the resolution stamp's shape).
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id="ev_ccccccccccc1",
            stance=EvidenceStance.SUPPORTS,
            reasoning="engine: user-confirmed resolution",
            linked_at_turn=3,
        )
    )
    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref=created[0],
                evidence_id="ev_ccccccccccc1",
                stance="refutes",
                reasoning="actually it came back",  # would flip the confirmation
            )
        ],
        current_turn=case.current_turn + 1,
    )
    links = [l for l in root.evidence_links if l.evidence_id == "ev_ccccccccccc1"]
    assert len(links) == 1
    assert links[0].stance == EvidenceStance.SUPPORTS  # verdict untouched


def test_omitted_confidence_on_reemission_preserves_prior_hedge():
    """An upsert that OMITS stance_confidence keeps the existing (possibly
    deliberately hedged) value — the schema default must not silently promote
    a considered 0.5 to full confidence and pull the link into the §7.1
    causal tally."""
    case = _case()
    seed_problem_node(case)
    case.evidence.append(_causal_row("ev_ddddddddddd1", "pool max_size dropped to 5"))
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_node("undersized connection pool", NodeType.ROOT, produces="D")],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref="new_index_0",
                evidence_id="ev_ddddddddddd1",
                stance="supports",
                reasoning="tentative",
                stance_confidence=0.5,  # deliberate hedge
            )
        ],
        current_turn=case.current_turn,
    )
    root = case.causal_nodes[created[0]]
    # Re-emission omitting the field (schema default None on the live path).
    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref=created[0],
                evidence_id="ev_ddddddddddd1",
                stance="supports",
                reasoning="re-listing the graph",
                stance_confidence=None,
            )
        ],
        current_turn=case.current_turn + 1,
    )
    assert root.evidence_links[0].stance_confidence == 0.5  # hedge preserved

    # Control: omitting the field on a NEW link still means full confidence.
    case.evidence.append(_causal_row("ev_ddddddddddd2", "wait queue saturation"))
    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[],
        node_evidence=[
            SimpleNamespace(
                node_ref=created[0],
                evidence_id="ev_ddddddddddd2",
                stance="supports",
                reasoning="fresh link",
                stance_confidence=None,
            )
        ],
        current_turn=case.current_turn + 1,
    )
    new_link = next(
        l for l in root.evidence_links if l.evidence_id == "ev_ddddddddddd2"
    )
    assert new_link.stance_confidence == 1.0
