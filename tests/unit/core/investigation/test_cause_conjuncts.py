"""#1096 — a cause established as a CONJUNCTION reaches the conclusion whole.

The conclusion mirror renders ONE chain (root -> ... -> D): the root becomes the
cause text, the intermediate rungs the mechanism. An M7 AND-set — the graph's
only representation of "the problem needed BOTH of these" — puts a co-necessary
cause OFF that chain, so before this it was established by the investigation and
absent from the conclusion, and the report published a two-factor cause as its
first factor alone.

Every assertion is a mechanical read of engine state over a hand-built graph:
nothing here depends on model behavior or wording.
"""

import hashlib
from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.causal_graph import (
    seed_problem_node,
    validated_and_conjuncts,
)
from faultmaven.core.investigation.milestone_engine import (
    _recompute_cause_state_from_chain,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit

_CACHE = "checkout-api v2.14.0 retains an unbounded orderSummaryCache"
_LIMIT = "the v2.14.0 release halved the checkout-api memory limit to 512Mi"
_HEAP = "JVM heap pressure causes GC pauses and readiness failure before OOM"

_A = "cn_0000000000aa"
_B = "cn_0000000000bb"
_M = "cn_0000000000cc"


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(label: str) -> Evidence:
    # Label embedded as content tokens so two rows read as INDEPENDENT
    # observations under the INV-29 mirror collapse.
    return Evidence(
        evidence_id=_eid(label),
        summary=f"fact-{label} metric-{label} reading-{label}",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _node(node_id, statement, node_type=NodeType.ROOT, *, supports=()) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=node_type,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=_eid(label),
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the node",
                linked_at_turn=2,
            )
            for label in supports
        ],
        generated_at_turn=1,
    )


def _conjunction_case(*, and_group: str | None = "g1", second_supports=("b1", "b2")):
    """A -> M <- B (co-necessary when ``and_group`` is set), M -> D. The
    hypothesis is rooted on A, so B is established only in the graph."""
    a = _node(_A, _CACHE, supports=["a1", "a2"])
    b = _node(_B, _LIMIT, supports=list(second_supports))
    m = _node(_M, _HEAP, node_type=NodeType.INTERMEDIATE)
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="checkout orders failing with 500s",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout orders failing with 500s",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.causal_nodes = {n.node_id: n for n in (a, b, m)}
    case.evidence = [_evidence(x) for x in ("a1", "a2", *second_supports)]
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=_A, effect_node_id=_M, and_group=and_group),
        CausalEdge(cause_node_id=_B, effect_node_id=_M, and_group=and_group),
        CausalEdge(cause_node_id=_M, effect_node_id=d.node_id),
    ]
    hyp = Hypothesis(
        hypothesis_id="hyp_0000000000aa",
        statement=_CACHE,
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=_A,
        path=[_A, _M, d.node_id],
        generated_at_turn=1,
    )
    case.hypotheses = {hyp.hypothesis_id: hyp}
    case.progress.symptom_verified = True
    return case, d


def test_mirror_names_the_validated_co_necessary_cause():
    """The whole point: the second conjunct is on the conclusion, not only the
    map — a report reader and a harvested runbook get both factors."""
    case, _d = _conjunction_case()
    _recompute_cause_state_from_chain(case)

    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.root_cause == _CACHE
    assert rcc.contributing_factors == [_LIMIT]


def test_an_unestablished_conjunct_is_not_asserted():
    """A single support holds B INCONCLUSIVE (INV-29). The conclusion must not
    name a factor the graph has not proved — the one-directional guarantee."""
    case, _d = _conjunction_case(second_supports=("b1",))
    _recompute_cause_state_from_chain(case)

    assert case.causal_nodes[_B].node_state != NodeState.VALIDATED
    assert case.root_cause_conclusion.contributing_factors == []


def test_an_or_alternative_is_not_a_conjunct():
    """Without ``and_group`` the two causes are independent alternatives, each
    sufficient on its own — naming B there would assert co-necessity the graph
    never claimed."""
    case, _d = _conjunction_case(and_group=None)
    _recompute_cause_state_from_chain(case)

    assert case.causal_nodes[_B].node_state == NodeState.VALIDATED
    assert case.root_cause_conclusion.contributing_factors == []


def test_chain_nodes_are_never_repeated_as_conjuncts():
    """The chain's own nodes are already the cause text and the mechanism."""
    case, d = _conjunction_case()
    _recompute_cause_state_from_chain(case)

    assert validated_and_conjuncts(case, [_A, _M, d.node_id]) == [_LIMIT]
    # Ask from B's side: A is co-necessary with it, and now A is the excluded
    # chain node — the exclusion is by membership, not by identity of the root.
    assert validated_and_conjuncts(case, [_B, _M, d.node_id]) == [_CACHE]


def test_a_conjunct_validating_later_refreshes_a_standing_mirror():
    """The faithfulness short-circuit returns early on a mirror whose root and
    grade still agree. A conjunct usually validates AFTER the root, so without
    the conjunct set in that check the mirror freezes at the single-factor text
    it was first minted with."""
    case, _d = _conjunction_case(second_supports=("b1",))
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion.contributing_factors == []

    # Second independent observation arrives; B validates.
    case.evidence.append(_evidence("b2"))
    case.causal_nodes[_B].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=_eid("b2"),
            stance=EvidenceStance.SUPPORTS,
            reasoning="bears on the node",
            linked_at_turn=6,
        )
    )
    _recompute_cause_state_from_chain(case)

    assert case.causal_nodes[_B].node_state == NodeState.VALIDATED
    assert case.root_cause_conclusion.contributing_factors == [_LIMIT]
