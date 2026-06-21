"""Unit tests for slice 1b: the chain-header / causal-graph additions to the
existing aggregate models (Two-Dimensional Hypothesis Methodology).

These are ADDITIVE fields — the flat-model fields (Hypothesis.statement,
Hypothesis.evidence_links, etc.) remain until the Phase-2 engine rewire. So
these tests assert the new fields exist with safe defaults and the light
chain-consistency validator, without disturbing flat-model behavior.

Design: docs/architecture/investigation-engine/
        two-dimensional-hypothesis-methodology.md §9.1
"""

import pytest
from pydantic import ValidationError

from faultmaven.modules.case.domain.models import (
    Case,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    InterventionQuadrant,
    Solution,
    SolutionType,
)

pytestmark = pytest.mark.unit


def _hyp(**overrides) -> Hypothesis:
    base = dict(
        statement="NetworkPolicy denies ingress to postgres",
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="initial",
    )
    base.update(overrides)
    return Hypothesis(**base)


# ---------------------------------------------------------------------------
# Hypothesis chain header
# ---------------------------------------------------------------------------


def test_hypothesis_chain_fields_default_empty():
    h = _hyp()
    assert h.root_node_id is None
    assert h.path == []
    # flat-model fields still present (transitional)
    assert h.statement
    assert h.evidence_links == []


def test_root_heads_path_ok():
    h = _hyp(
        root_node_id="cn_000000000001", path=["cn_000000000001", "cn_0000000000d0"]
    )
    assert h.root_node_id == h.path[0]


def test_root_mismatch_rejected():
    with pytest.raises(ValidationError, match=r"root_node_id must equal path\[0\]"):
        _hyp(
            root_node_id="cn_000000000001",
            path=["cn_999999999999", "cn_0000000000d0"],
        )


def test_root_set_empty_path_is_lenient():
    # mid-expansion: a root posited before the full path is materialized
    h = _hyp(root_node_id="cn_000000000001", path=[])
    assert h.root_node_id == "cn_000000000001"
    assert h.path == []


def test_path_without_root_is_lenient():
    # a partial chain still descending toward a root
    h = _hyp(path=["cn_0000000000d0"])
    assert h.root_node_id is None
    assert h.path == ["cn_0000000000d0"]


# ---------------------------------------------------------------------------
# Solution causal-graph linkage
# ---------------------------------------------------------------------------


def test_solution_linkage_fields_default_none():
    s = Solution(
        solution_type=SolutionType.CONFIG_CHANGE,
        title="Add ingress from-clause",
        immediate_action="patch the NetworkPolicy",
    )
    assert s.node_id is None
    assert s.quadrant is None


def test_solution_with_node_and_quadrant():
    s = Solution(
        solution_type=SolutionType.CONFIG_CHANGE,
        title="Add ingress from-clause",
        immediate_action="patch the NetworkPolicy",
        node_id="cn_000000000001",
        quadrant=InterventionQuadrant.REMEDIATION,
    )
    assert s.node_id == "cn_000000000001"
    assert s.quadrant == InterventionQuadrant.REMEDIATION


# ---------------------------------------------------------------------------
# Case causal-graph collections (introspected to avoid heavy construction)
# ---------------------------------------------------------------------------


def test_case_has_graph_collections_with_empty_defaults():
    fields = Case.model_fields
    assert "causal_nodes" in fields
    assert "causal_edges" in fields
    # default factories produce empty containers
    assert fields["causal_nodes"].default_factory() == {}
    assert fields["causal_edges"].default_factory() == []
