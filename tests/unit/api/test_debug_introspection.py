"""Unit tests for the debug causal-graph payload builder.

The fm-sre-simulator chain probe consumes this payload to validate the engine's
causal graph from the outside: hypotheses (with their ``rationale`` and chain
link), nodes/edges, and the engine-derived ``cause_state``.
"""

from types import SimpleNamespace

import pytest

from faultmaven.api.debug_introspection import build_causal_graph_debug_payload
from faultmaven.modules.case.domain.models import (
    CausalEdge,
    CausalNode,
    CauseState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NodeType,
)

pytestmark = pytest.mark.unit


def _hyp(statement: str, rationale: str, **kw) -> Hypothesis:
    kw.setdefault("generated_at_turn", 1)
    kw.setdefault("generation_mode", HypothesisGenerationMode.OPPORTUNISTIC)
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.OTHER,
        rationale=rationale,
        **kw,
    )


def _case(hypotheses: dict) -> SimpleNamespace:
    # The builder is duck-typed; a namespace with the read attrs is enough.
    return SimpleNamespace(
        case_id="case_abc123",
        current_turn=3,
        causal_nodes={},
        causal_edges=[],
        hypotheses=hypotheses,
        progress=None,
        root_cause_conclusion=None,
    )


class TestRationaleExposure:
    def test_hypothesis_rationale_is_exposed_verbatim(self):
        rationale = "Derived from the user's stack trace"
        h = _hyp("The PVC has no matching StorageClass", rationale)
        payload = build_causal_graph_debug_payload(_case({h.hypothesis_id: h}))

        dumped = payload["hypotheses"][h.hypothesis_id]
        assert dumped["rationale"] == rationale


class TestPayloadShape:
    def test_core_fields_and_chain_link_present(self):
        h = _hyp(
            "cause",
            "why",
            likelihood=0.5,
            initial_likelihood=0.5,
            state=HypothesisState.ACTIVE,
            root_node_id="cn_root1",
        )
        payload = build_causal_graph_debug_payload(_case({h.hypothesis_id: h}))

        assert payload["case_id"] == "case_abc123"
        assert payload["current_turn"] == 3
        assert payload["cause_state"] is None  # progress=None
        dumped = payload["hypotheses"][h.hypothesis_id]
        # Chain link + capped-prior soundness fields the sim asserts on.
        assert dumped["root_node_id"] == "cn_root1"
        assert dumped["likelihood"] == 0.5
        assert dumped["initial_likelihood"] == 0.5
        assert dumped["state"] == "active"

    def test_empty_graph_serializes_clean(self):
        payload = build_causal_graph_debug_payload(_case({}))
        assert payload["hypotheses"] == {}
        assert payload["causal_nodes"] == {}
        assert payload["causal_edges"] == []
        assert payload["problem_node_id"] is None
        assert payload["root_cause_conclusion"] is None

    def test_exposes_exact_keyset(self):
        # Pin the full key set so a future edit dropping a field is caught.
        payload = build_causal_graph_debug_payload(_case({}))
        assert set(payload) == {
            "case_id",
            "current_turn",
            "cause_state",
            "problem_node_id",
            "causal_nodes",
            "causal_edges",
            "hypotheses",
            "root_cause_conclusion",
        }


class TestRealGraphSerialization:
    """Exercise the real CausalNode/CausalEdge/cause_state paths the probe reads —
    not just empty collections — including node ``metadata`` round-trip."""

    def test_real_nodes_edges_and_cause_state_serialize(self):
        d = CausalNode(
            statement="PVC stays Pending — no volume provisioned",
            node_type=NodeType.PROBLEM,
            generated_at_turn=1,
        )
        root = CausalNode(
            statement="Referenced StorageClass is missing",
            node_type=NodeType.ROOT,
            generated_at_turn=1,
            metadata={"origin": "chain-emission"},
        )
        edge = CausalEdge(
            cause_node_id=root.node_id,
            effect_node_id=d.node_id,
            created_at_turn=1,
        )
        case = _case({})
        case.causal_nodes = {d.node_id: d, root.node_id: root}
        case.causal_edges = [edge]
        case.progress = SimpleNamespace(cause_state=CauseState.CANDIDATES)

        payload = build_causal_graph_debug_payload(case)

        # Problem-node finder picks the PROBLEM (D) node.
        assert payload["problem_node_id"] == d.node_id
        # Enums serialize to their string values (model_dump mode="json").
        assert payload["causal_nodes"][d.node_id]["node_type"] == "problem"
        assert payload["cause_state"] == "candidates"
        # Edge round-trips with the right endpoints.
        assert len(payload["causal_edges"]) == 1
        assert payload["causal_edges"][0]["cause_node_id"] == root.node_id
        assert payload["causal_edges"][0]["effect_node_id"] == d.node_id
        # Node metadata survives the dump.
        assert payload["causal_nodes"][root.node_id]["metadata"] == {
            "origin": "chain-emission"
        }
