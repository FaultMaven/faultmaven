"""PR B2b: flag-gated chain-emission prompt block + engine wiring.

The prompt block is appended to the DIAGNOSIS instructions only when
``enable_hypothesis_chain_emission`` is on (baseline unchanged when off);
``_apply_chain_emission`` ingests the emitted chain and links each hypothesis
to its root.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.prompts.templates import _select_diagnosis_block
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeType,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


def _case() -> Case:
    case = Case(
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
        problem_verification=ProblemVerification(
            symptom_statement="Deploy to on-prem job fails",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 4
    return case


# ---------------------------------------------------------------------------
# Prompt gating — the only baseline-affecting surface
# ---------------------------------------------------------------------------


def test_diagnosis_prompt_excludes_chain_block_when_flag_off():
    # Default (flag off): the carefully-tuned baseline prompt is unchanged.
    block = _select_diagnosis_block(_case())
    assert "CAUSAL CHAINS" not in block


def test_diagnosis_prompt_includes_chain_block_when_flag_on(monkeypatch):
    import faultmaven.config.settings as settings_mod

    stub = SimpleNamespace(
        features=SimpleNamespace(enable_hypothesis_chain_emission=True)
    )
    monkeypatch.setattr(settings_mod, "get_settings", lambda: stub)

    block = _select_diagnosis_block(_case())
    assert "CAUSAL CHAINS" in block
    # Step 3 spells out every required NodeEvidenceLinkToAdd field (the alignment).
    assert "node_evidence_links" in block
    assert "evidence_id_ref" in block


# ---------------------------------------------------------------------------
# Engine wiring — _apply_chain_emission
# ---------------------------------------------------------------------------


def _engine() -> MilestoneEngine:
    # _apply_chain_emission uses only module functions + the args; no deps needed.
    return MilestoneEngine.__new__(MilestoneEngine)


def _hyp() -> Hypothesis:
    return Hypothesis(
        statement="NetworkPolicy blocks the connection",
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=4,
        rationale="r",
        state=HypothesisState.ACTIVE,
    )


def _updates(nodes=None, edges=None, node_evidence=None):
    return SimpleNamespace(
        causal_nodes_to_add=nodes or [],
        causal_edges_to_add=edges or [],
        node_evidence_links=node_evidence or [],
    )


def test_apply_chain_emission_links_hypothesis_to_root_and_path():
    eng = _engine()
    case = _case()
    h = _hyp()
    case.hypotheses = {h.hypothesis_id: h}
    # The hyp->ref map is recorded at hypothesis creation (section 3); no
    # positional zip against the spec list.
    metadata = {
        "hypotheses_generated": [h.hypothesis_id],
        "hyp_root_refs": {h.hypothesis_id: "new_index_0"},
    }
    updates = _updates(
        nodes=[
            SimpleNamespace(
                statement="NetworkPolicy denies ingress",
                node_type=NodeType.ROOT,
                produces="D",
                and_group=None,
            )
        ]
    )

    eng._apply_chain_emission(case, updates, metadata)

    assert h.root_node_id is not None
    d_id = next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    assert h.path == [h.root_node_id, d_id]
    assert case.causal_nodes[h.root_node_id].node_type == NodeType.ROOT


def test_apply_chain_emission_leaves_hypothesis_flat_when_ref_unresolvable():
    # An unresolvable root_node_ref leaves the hypothesis flat (the bridge floor
    # will degenerate-project it) rather than raising.
    eng = _engine()
    case = _case()
    h = _hyp()
    case.hypotheses = {h.hypothesis_id: h}
    metadata = {
        "hypotheses_generated": [h.hypothesis_id],
        "hyp_root_refs": {h.hypothesis_id: "new_index_9"},  # out of range
    }

    eng._apply_chain_emission(case, _updates(), metadata)

    assert h.root_node_id is None


def test_apply_chain_emission_rejects_non_root_ref():
    # root_node_ref must resolve to a ROOT node (M1/M3). An intermediate node is
    # rejected — the hypothesis stays flat rather than being rooted mid-chain.
    eng = _engine()
    case = _case()
    h = _hyp()
    case.hypotheses = {h.hypothesis_id: h}
    metadata = {
        "hypotheses_generated": [h.hypothesis_id],
        "hyp_root_refs": {h.hypothesis_id: "new_index_0"},
    }
    updates = _updates(
        nodes=[
            SimpleNamespace(
                statement="connection refused",
                node_type=NodeType.INTERMEDIATE,  # not a ROOT
                produces="D",
                and_group=None,
            )
        ]
    )

    eng._apply_chain_emission(case, updates, metadata)

    assert h.root_node_id is None  # intermediate ref rejected


def test_apply_chain_emission_noop_when_no_root_ref():
    # A flat hypothesis with no recorded ref is untouched (stays flat).
    eng = _engine()
    case = _case()
    h = _hyp()
    case.hypotheses = {h.hypothesis_id: h}
    metadata = {"hypotheses_generated": [h.hypothesis_id]}  # no hyp_root_refs

    eng._apply_chain_emission(case, _updates(), metadata)

    assert h.root_node_id is None
    assert h.path == []


# ---------------------------------------------------------------------------
# Cross-turn RE-ROOT — elaborating a bridge-stubbed hypothesis into a real chain
# must move the hypothesis onto that chain AND garbage-collect the abandoned
# degenerate stub, so the chain does not co-exist with the stub (the orphan-chain
# / double-representation divergence).
# ---------------------------------------------------------------------------


def _two_rung_chain():
    # root -> intermediate -> D
    return _updates(
        nodes=[
            SimpleNamespace(
                statement="deploy dropped `defer conn.Release()`",
                node_type=NodeType.ROOT,
                produces="new_index_1",
                and_group=None,
            ),
            SimpleNamespace(
                statement="connections are acquired but never released",
                node_type=NodeType.INTERMEDIATE,
                produces="D",
                and_group=None,
            ),
        ]
    )


def test_reroot_moves_hypothesis_to_chain_and_gcs_old_stub():
    from _bridge_fixture import bridge_flat_hypotheses_to_graph

    eng = _engine()
    case = _case()
    h = _hyp()
    case.hypotheses = {h.hypothesis_id: h}

    # Posit-time: the bridge degenerate-projects the flat hypothesis to a stub
    # root (root -> D), exactly as it does on the turn the hypothesis is created.
    bridge_flat_hypotheses_to_graph(case)
    stub_id = h.root_node_id
    assert stub_id is not None
    assert h.path == [stub_id, _problem_id(case)]

    # Elaboration turn: the LLM emits the real chain and RE-ROOTS the existing
    # hypothesis onto it (root_node_ref carried on a hypotheses_to_update entry,
    # recorded into hyp_root_refs by _apply_hypothesis_updates).
    metadata = {
        "hypotheses_generated": [],
        "hyp_root_refs": {h.hypothesis_id: "new_index_0"},
    }
    eng._apply_chain_emission(case, _two_rung_chain(), metadata)

    d_id = _problem_id(case)
    new_root = h.root_node_id
    # Re-rooted onto the emitted chain (a genuine multi-rung path, not a stub).
    assert new_root != stub_id
    assert case.causal_nodes[new_root].node_type == NodeType.ROOT
    assert h.path[0] == new_root and h.path[-1] == d_id
    assert len(h.path) == 3  # root -> intermediate -> D
    # The abandoned stub is gone — no orphan, no double-representation.
    assert stub_id not in case.causal_nodes
    assert all(
        e.cause_node_id != stub_id and e.effect_node_id != stub_id
        for e in case.causal_edges
    )
    # Every emitted node is on the hypothesis path (no orphan emitted nodes).
    on_path = set(h.path)
    assert all(nid in on_path for nid in case.causal_nodes)


def test_reroot_keeps_old_root_when_another_hypothesis_still_uses_it():
    # The stub GC is conservative: a root still referenced by another hypothesis
    # is load-bearing and must NOT be collected when one hypothesis re-roots away.
    from _bridge_fixture import bridge_flat_hypotheses_to_graph

    eng = _engine()
    case = _case()
    h1 = _hyp()
    h2 = _hyp()
    case.hypotheses = {h1.hypothesis_id: h1, h2.hypothesis_id: h2}
    bridge_flat_hypotheses_to_graph(case)
    # Force the shared-root edge case: point h2 at h1's stub root.
    shared_root = h1.root_node_id
    h2.root_node_id = shared_root
    h2.path = list(h1.path)

    metadata = {
        "hypotheses_generated": [],
        "hyp_root_refs": {h1.hypothesis_id: "new_index_0"},
    }
    eng._apply_chain_emission(case, _two_rung_chain(), metadata)

    # h1 moved to the new chain; the shared stub stays because h2 still uses it.
    assert h1.root_node_id != shared_root
    assert shared_root in case.causal_nodes
    assert h2.root_node_id == shared_root


def _problem_id(case) -> str:
    return next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )


def test_reroot_with_incomplete_chain_keeps_existing_link():
    # A re-root onto a chain that does NOT yet reach D (chain_path_to_problem
    # returns []) must NOT abandon the hypothesis's working bridge link: doing so
    # would strand it (the bridge floor that runs next skips a hyp with a
    # root_node_id, so nothing restores the link). The existing root/path and the
    # old stub are preserved until the chain is actually complete.
    from _bridge_fixture import bridge_flat_hypotheses_to_graph

    eng = _engine()
    case = _case()
    h = _hyp()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    stub_id = h.root_node_id
    d_id = _problem_id(case)
    assert h.path == [stub_id, d_id]

    # Emit a root whose `produces` dangles (never reaches D) -> open chain.
    incomplete = _updates(
        nodes=[
            SimpleNamespace(
                statement="partial root, mechanism not yet traced to D",
                node_type=NodeType.ROOT,
                produces="new_index_9",  # out of range -> no edge, no path to D
                and_group=None,
            )
        ]
    )
    metadata = {
        "hypotheses_generated": [],
        "hyp_root_refs": {h.hypothesis_id: "new_index_0"},
    }
    eng._apply_chain_emission(case, incomplete, metadata)

    # Re-root deferred: the hypothesis keeps its working [stub, D] link.
    assert h.root_node_id == stub_id
    assert h.path == [stub_id, d_id]
    assert stub_id in case.causal_nodes


def test_reroot_gcs_full_abandoned_multi_rung_chain():
    # Re-rooting away from a REAL multi-rung chain (root -> intermediate -> D)
    # must collect the WHOLE abandoned chain, not just its root — otherwise the
    # old intermediate is left orphaned (the divergence one hop up).
    eng = _engine()
    case = _case()
    h = _hyp()
    case.hypotheses = {h.hypothesis_id: h}

    # First: link the hypothesis onto a 2-rung chain (root -> intermediate -> D).
    eng._apply_chain_emission(
        case,
        _two_rung_chain(),
        {"hypotheses_generated": [], "hyp_root_refs": {h.hypothesis_id: "new_index_0"}},
    )
    d_id = _problem_id(case)
    old_root, old_mid = h.path[0], h.path[1]
    assert h.path == [old_root, old_mid, d_id]

    # Then: re-root onto a different fresh root that reaches D directly.
    eng._apply_chain_emission(
        case,
        _updates(
            nodes=[
                SimpleNamespace(
                    statement="the real deeper root",
                    node_type=NodeType.ROOT,
                    produces="D",
                    and_group=None,
                )
            ]
        ),
        {"hypotheses_generated": [], "hyp_root_refs": {h.hypothesis_id: "new_index_0"}},
    )

    new_root = h.root_node_id
    assert new_root not in (old_root, old_mid)
    assert h.path == [new_root, d_id]
    # The entire abandoned chain is collected; D survives.
    assert old_root not in case.causal_nodes
    assert old_mid not in case.causal_nodes
    assert d_id in case.causal_nodes
    # No dangling edges to collected nodes.
    assert all(
        e.cause_node_id in case.causal_nodes and e.effect_node_id in case.causal_nodes
        for e in case.causal_edges
    )
