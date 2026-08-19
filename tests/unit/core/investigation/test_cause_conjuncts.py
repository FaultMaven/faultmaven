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
    conjuncts_for_chain,
    incoming_and_groups,
    ingest_emitted_chain,
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
    CauseState,
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


# ---------------------------------------------------------------------------
# Hardening the derivation's edges (review of the first cut)
# ---------------------------------------------------------------------------


def test_conjuncts_do_not_depend_on_edge_row_order():
    """Neither repository loads ``causal_edges`` with an ORDER BY, so a
    row-order-derived list would vary per fetch on PostgreSQL — re-minting the
    conclusion every recompute and flipping the report's bullets."""
    case, _d = _conjunction_case()
    third = _node(
        "cn_0000000000dd", "a third co-necessary condition", supports=["c1", "c2"]
    )
    case.causal_nodes[third.node_id] = third
    case.evidence += [_evidence("c1"), _evidence("c2")]
    case.causal_edges.append(
        CausalEdge(cause_node_id=third.node_id, effect_node_id=_M, and_group="g1")
    )
    _recompute_cause_state_from_chain(case)
    first = list(case.root_cause_conclusion.contributing_factors)
    assert len(first) == 2

    case.causal_edges.reverse()
    case.root_cause_conclusion = None
    _recompute_cause_state_from_chain(case)
    assert list(case.root_cause_conclusion.contributing_factors) == first


def test_a_blank_and_group_is_not_a_conjunction():
    """``and_group`` is an unconstrained Optional[str] end to end. A model
    emitting "" on independent alternatives must not have them published as
    conditions the cause required."""
    from faultmaven.core.investigation.causal_graph import ingest_emitted_chain

    case, d = _conjunction_case(and_group=None)
    case.causal_edges = [e for e in case.causal_edges if e.cause_node_id != _B]

    class _Spec:
        statement = "an independent alternative cause"
        node_type = "root"
        produces = _M
        and_group = "   "

    ingest_emitted_chain(
        case,
        nodes_to_add=[_Spec()],
        edges_to_add=[],
        node_evidence=[],
        current_turn=6,
    )
    added = [e for e in case.causal_edges if e.effect_node_id == _M]
    assert added and all(e.and_group is None for e in added)


def test_a_path_less_hypothesis_still_sees_the_and_set_on_the_problem():
    """The canonical two-factor shape points both conjuncts straight at D, so
    reading the root's incoming edges alone would report none."""
    case, d = _conjunction_case()
    case.causal_edges = [
        CausalEdge(cause_node_id=_A, effect_node_id=d.node_id, and_group="g1"),
        CausalEdge(cause_node_id=_B, effect_node_id=d.node_id, and_group="g1"),
    ]
    hyp = case.hypotheses["hyp_0000000000aa"]
    hyp.path = []
    _recompute_cause_state_from_chain(case)

    assert case.root_cause_conclusion.contributing_factors == [_LIMIT]


def _confirm_root(case, node_id: str, label: str) -> None:
    """Stamp a counterfactual (gone=>gone) confirmation on a root. The engine is
    the only live producer of such a link; the fixture writes it directly."""
    case.evidence.append(
        Evidence(
            evidence_id=_eid(label),
            summary=f"the cause is absent after the fix ({label})",
            primary_purpose="diagnosis",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="engine",
            collected_at_turn=7,
            collected_at=datetime.now(timezone.utc),
        )
    )
    case.causal_nodes[node_id].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=_eid(label),
            stance=EvidenceStance.SUPPORTS,
            reasoning="removing the cause removed the problem",
            linked_at_turn=7,
        )
    )


def test_a_conjunct_refresh_does_not_swap_the_published_cause():
    """Two counterfactually CONFIRMED roots settle the MECE contest, so both
    stand. A conjunct-driven re-mint is a refresh for a reason unrelated to the
    cause: it must keep the cause the standing mirror already names, or the
    published root_cause changes because a conjunct validated."""
    from faultmaven.core.investigation.causal_graph import (
        synthesize_rcc_from_validated_root,
    )

    case, _d = _conjunction_case()
    d_id = _d_id(case)
    _confirm_root(case, _A, "abs_a")
    _recompute_cause_state_from_chain(case)
    named = case.root_cause_conclusion.root_cause
    assert named == _CACHE

    # A second confirmed cause arrives, ordered FIRST in the standing-hypothesis
    # iteration — so confirmed_hyps[0] is NOT the prior mirror's own root.
    rival = _node("cn_0000000000ee", "a rival standing cause", supports=["r1", "r2"])
    case.causal_nodes["cn_0000000000ee"] = rival
    case.evidence += [_evidence("r1"), _evidence("r2")]
    case.causal_edges.append(
        CausalEdge(cause_node_id=rival.node_id, effect_node_id=d_id)
    )
    rival_hyp = Hypothesis(
        hypothesis_id="hyp_0000000000cc",
        statement="a rival standing cause",
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="rival",
        root_node_id=rival.node_id,
        path=[rival.node_id, d_id],
        generated_at_turn=1,
    )
    case.hypotheses = {rival_hyp.hypothesis_id: rival_hyp, **case.hypotheses}
    _confirm_root(case, rival.node_id, "abs_r")
    # Derive the rival's node state. The mirror short-circuits here (root and
    # grade still agree, conjuncts unchanged), so the cache root stays named.
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion.root_cause == named

    # Invalidate ONLY the conjunct set, so nothing else can drive the re-mint.
    case.root_cause_conclusion.contributing_factors = ["stale"]
    synthesize_rcc_from_validated_root(case)

    assert case.root_cause_conclusion.root_cause == named


def _d_id(case) -> str:
    return next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )


def test_the_confirm_stamp_degrades_when_the_graph_hook_is_missing():
    """The stamp runs on the unguarded RESOLVED-execution path, so reading the
    conjunct hook must degrade to naming none — never a KeyError that 500s the
    transition (same discipline as the arbitration hook beside it)."""
    from unittest.mock import patch

    from faultmaven.core.investigation import cause_assurance

    case, _d = _conjunction_case()
    _recompute_cause_state_from_chain(case)
    root = case.causal_nodes[_A]
    hyp = case.hypotheses["hyp_0000000000aa"]
    with patch.dict(cause_assurance._GRAPH_HOOKS, {}, clear=True):
        assert cause_assurance._conjuncts_for_root(case, root, hyp) == []


def test_a_blank_and_group_persisted_before_the_ingest_guard_is_not_a_conjunction():
    """Read-side twin of the ingest test. ``_add_edge`` cannot reach rows already
    stored with "", and those are exactly the rows the guard was written for —
    so the normalization has to hold where the graph is READ, or a legacy case
    publishes co-necessity over independent alternatives."""
    case, _d = _conjunction_case(and_group=None)
    # Simulate the persisted shape: a stored edge whose and_group is blank.
    for edge in case.causal_edges:
        if edge.effect_node_id == _M:
            edge.and_group = ""
    _recompute_cause_state_from_chain(case)

    assert case.causal_nodes[_B].node_state == NodeState.VALIDATED
    assert case.root_cause_conclusion.contributing_factors == []


def test_both_mint_sites_build_the_same_chain():
    """``causal_graph`` (per-turn mirror) and ``cause_assurance`` (terminal
    confirm-stamp) must name the SAME conjuncts for one case. They sit either
    side of a module boundary the stamp can only cross by hook, so this pins
    that there is one chain-builder rather than two copies free to drift."""
    from faultmaven.core.investigation import cause_assurance

    case, _d = _conjunction_case()
    _recompute_cause_state_from_chain(case)
    hyp = case.hypotheses["hyp_0000000000aa"]
    root = case.causal_nodes[_A]

    assert cause_assurance._conjuncts_for_root(case, root, hyp) == conjuncts_for_chain(
        case, hyp
    )
    # ...including on the path-less fallback, where the rule is least obvious.
    hyp.path = []
    assert cause_assurance._conjuncts_for_root(case, root, hyp) == conjuncts_for_chain(
        case, hyp
    )


# ---------------------------------------------------------------------------
# §7.1.2 arbitration — a conjunction is not a differential (#1096, second half)
#
# #1102 taught the prompt to model a two-condition cause as an AND-set, and the
# renderer to name the whole conjunction. The arbitration lane still read S2
# literally — "roots are mutually-exclusive origins, at most one can be the
# cause" — so two VALIDATED conjuncts registered as a MECE contest: no
# cause_state=IDENTIFIED (hence no M5 solution license), no conclusion minted at
# all, and a refused resolution confirm-stamp. Modelling the cause correctly was
# worse than compressing it into one node, which is what the reported case did.
# ---------------------------------------------------------------------------


def _hypothesis_for(case, d, node_id: str, statement: str, hyp_id: str) -> None:
    """Give a conjunct its own standing hypothesis — the shape that arises when
    the investigation tests each condition as its own theory. Only a STANDING
    hypothesis's root can enter the contest, so this is what exposes it."""
    case.hypotheses[hyp_id] = Hypothesis(
        hypothesis_id=hyp_id,
        statement=statement,
        category=HypothesisCategory.CONFIG,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="tested as its own theory",
        root_node_id=node_id,
        path=[node_id, _M, d.node_id],
        generated_at_turn=1,
    )


def test_co_necessary_conjuncts_are_not_a_mece_contest():
    """Both conditions proven is the CORRECT end state for a conjunction, not
    the "several simultaneously-proven exclusive causes" a hold exists to catch.
    Without this the engine held identification on the very shape it asks for."""
    from faultmaven.core.investigation.causal_graph import (
        distinct_cause_clusters,
        mece_contested_root_ids,
    )

    case, d = _conjunction_case()
    _hypothesis_for(case, d, _B, _LIMIT, "hyp_0000000000bb")
    _recompute_cause_state_from_chain(case)

    assert case.causal_nodes[_A].node_state == NodeState.VALIDATED
    assert case.causal_nodes[_B].node_state == NodeState.VALIDATED
    assert distinct_cause_clusters(case, {_A, _B}) == [{_A, _B}]
    assert mece_contested_root_ids(case) == set()
    # ...and the consequences that hung off the hold: identification stands and
    # the conclusion is minted, naming the whole conjunction.
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.root_cause_conclusion is not None
    assert case.root_cause_conclusion.contributing_factors == [_LIMIT]


def test_a_conjunction_does_not_refuse_the_resolution_confirm_stamp():
    """The stamp arbitrates before citing a root: two conjuncts read as two
    distinct causes made it refuse, so a correctly-modelled two-factor cause
    could never reach the CONFIRMED grade (and harvest stayed blocked)."""
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
        evidence_category_map,
        root_counterfactually_confirmed,
    )

    case, d = _conjunction_case()
    _hypothesis_for(case, d, _B, _LIMIT, "hyp_0000000000bb")
    _recompute_cause_state_from_chain(case)

    case.evidence.append(
        Evidence(
            evidence_id=_eid("resolution_absence"),
            summary=(
                "after the fix the cache is bounded and the limit restored; "
                "the OOM crash-loop signature is gone"
            ),
            primary_purpose="diagnosis",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="llm",
            collected_at_turn=8,
            collected_at=datetime.now(timezone.utc),
        )
    )
    case.current_turn = 8

    assert confirm_root_from_resolution_absence(case) is True
    cat_by_id = evidence_category_map(case)
    # ONE member of the conjunction carries the citation (the stamp cites a
    # single origin by design); cluster-wide idempotence makes that the
    # CAUSE's confirmation, and the conjunct is still named on the conclusion.
    assert root_counterfactually_confirmed(case.causal_nodes[_A], cat_by_id)
    assert case.root_cause_conclusion.contributing_factors == [_LIMIT]


def test_independent_alternatives_still_contest():
    """The guard this must not blunt: without ``and_group`` the two roots are
    competing explanations, and two of them simultaneously validated is exactly
    the coherence violation §7.1.2 holds identification on."""
    from faultmaven.core.investigation.causal_graph import mece_contested_root_ids

    case, d = _conjunction_case(and_group=None)
    _hypothesis_for(case, d, _B, _LIMIT, "hyp_0000000000bb")
    _recompute_cause_state_from_chain(case)

    assert mece_contested_root_ids(case) == {_A, _B}
    assert case.progress.cause_state != CauseState.IDENTIFIED


def test_a_blank_and_group_does_not_fuse_competing_causes():
    """Legacy rows carry ``and_group=""`` (unreachable by the ingest guard). A
    blank key names no group, so it must not merge a real differential into one
    cluster and silently retire the hold."""
    from faultmaven.core.investigation.causal_graph import mece_contested_root_ids

    case, d = _conjunction_case(and_group=None)
    for edge in case.causal_edges:
        if edge.effect_node_id == _M:
            edge.and_group = ""
    _hypothesis_for(case, d, _B, _LIMIT, "hyp_0000000000bb")
    _recompute_cause_state_from_chain(case)

    assert mece_contested_root_ids(case) == {_A, _B}


def test_an_and_set_beside_an_independent_alternative_still_contests():
    """The merge is of the CONJUNCTS only. "A and B together" versus "C" is a
    genuine differential, and the engine must still refuse to pick."""
    from faultmaven.core.investigation.causal_graph import (
        distinct_cause_clusters,
        mece_contested_root_ids,
    )

    case, d = _conjunction_case()
    rival_id = "cn_0000000000ff"
    rival = _node(
        rival_id, "an unrelated broker outage drops the orders", supports=["c1", "c2"]
    )
    case.causal_nodes[rival_id] = rival
    case.evidence += [_evidence("c1"), _evidence("c2")]
    case.causal_edges.append(
        CausalEdge(cause_node_id=rival_id, effect_node_id=d.node_id)
    )
    _hypothesis_for(case, d, _B, _LIMIT, "hyp_0000000000bb")
    case.hypotheses["hyp_0000000000cc"] = Hypothesis(
        hypothesis_id="hyp_0000000000cc",
        statement=rival.statement,
        category=HypothesisCategory.EXTERNAL,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="rival",
        root_node_id=rival_id,
        path=[rival_id, d.node_id],
        generated_at_turn=1,
    )
    _recompute_cause_state_from_chain(case)

    assert distinct_cause_clusters(case, {_A, _B, rival_id}) == [{_A, _B}, {rival_id}]
    assert mece_contested_root_ids(case) == {_A, _B, rival_id}
    assert case.progress.cause_state != CauseState.IDENTIFIED


# ---------------------------------------------------------------------------
# Reaching the AND-set at all — the two doors that closed before the graph
# could hold a conjunction (review of the arbitration fix).
# ---------------------------------------------------------------------------


class _EmittedEdge:
    """An explicit ``edges_to_add`` entry, duck-typed as the ingest reads it."""

    def __init__(self, cause: str, effect: str, and_group: str | None):
        self.cause = cause
        self.effect = effect
        self.and_group = and_group
        self.reasoning = "co-necessary"


def test_an_existing_edge_can_gain_an_and_group():
    """Co-necessity is usually recognized AFTER the fact: the model proposes
    two independent candidates and only later sees the problem needed both.
    Expressing that means re-emitting the edges with a shared group, and the
    flat idempotence drop left the AND-set half-formed — so no conjunction ever
    existed to render, the #1096 factor loss through a second door."""
    case, _d = _conjunction_case(and_group=None)
    assert incoming_and_groups(_M, case.causal_edges) == {None: [_A, _B]}

    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[_EmittedEdge(_A, _M, "g1"), _EmittedEdge(_B, _M, "g1")],
        node_evidence=[],
        current_turn=6,
    )

    assert incoming_and_groups(_M, case.causal_edges) == {"g1": [_A, _B]}
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion.contributing_factors == [_LIMIT]


def test_regrouping_is_monotone():
    """An edge may go None -> group, never group -> another group (a silent
    regrouping of a standing conjunction) and never group -> None (a later
    ungrouped restatement is not a retraction; treating it as one would make
    the published conjunction flicker turn to turn)."""
    case, _d = _conjunction_case()  # already grouped "g1"

    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[_EmittedEdge(_A, _M, "g2"), _EmittedEdge(_B, _M, None)],
        node_evidence=[],
        current_turn=6,
    )

    assert incoming_and_groups(_M, case.causal_edges) == {"g1": [_A, _B]}


def test_an_over_long_and_group_is_folded_to_fit_its_column():
    """``causal_edges.and_group`` is String(64) while the field is unconstrained
    at every layer above it, and the #1096 prompt invites a DESCRIPTIVE key. An
    over-long one inserts fine on SQLite and raises on PostgreSQL — a
    cloud-only failure of the case save."""
    long_key = "memory-exhaustion-requires-unbounded-cache-and-reduced-memory-limit"
    assert len(long_key) > 64

    case, _d = _conjunction_case(and_group=None)
    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[
            _EmittedEdge(_A, _M, long_key),
            _EmittedEdge(_B, _M, long_key),
        ],
        node_evidence=[],
        current_turn=6,
    )

    groups = incoming_and_groups(_M, case.causal_edges)
    (key,) = groups
    assert len(key) <= 64
    # Still ONE group: a fold that split the members would lose the AND-set.
    assert sorted(groups[key]) == sorted([_A, _B])


def test_the_fold_does_not_merge_distinct_long_keys():
    """A plain truncation would make two long keys sharing a 64-char prefix one
    group — the silent M7 strengthening the blank-key guard exists to prevent,
    by another route. The fold is also a pure function of the key, so the same
    logical group emitted a turn later normalizes to the same token."""
    from faultmaven.core.investigation.causal_graph import _normalize_and_group

    a = "memory-exhaustion-requires-unbounded-cache-and-reduced-memory-limit"
    b = "memory-exhaustion-requires-unbounded-cache-and-reduced-cpu-quota-only"

    assert _normalize_and_group(a) != _normalize_and_group(b)
    assert _normalize_and_group(a) == _normalize_and_group(a)
    assert _normalize_and_group("   ") is None
    assert _normalize_and_group(None) is None


# ---------------------------------------------------------------------------
# Observability of the trust grant (adversarial review of the arbitration fix).
#
# Since a conjunction is no longer a contest, an `and_group` emitted over two
# ALREADY-VALIDATED rivals dissolves an arbitration hold — permanently, because
# the merge is monotone. Honoring it is the intended design (the model authors
# causal structure everywhere else), but the sequence must leave a trace.
# ---------------------------------------------------------------------------


def test_a_late_grouping_over_validated_rivals_is_counted():
    """The suspicious sequence: both causes validate as rivals, and only THEN
    does a grouping token arrive that makes them one conjunctive cause."""
    from unittest.mock import patch

    from faultmaven.core.investigation import causal_graph
    from faultmaven.core.investigation.causal_graph import mece_contested_root_ids

    case, d = _conjunction_case(and_group=None)
    _hypothesis_for(case, d, _B, _LIMIT, "hyp_0000000000bb")
    _recompute_cause_state_from_chain(case)
    # They are rivals, and the engine is holding identification.
    assert mece_contested_root_ids(case) == {_A, _B}

    with patch.object(causal_graph, "causal_and_set_late_grouping_total") as ctr:
        ingest_emitted_chain(
            case,
            nodes_to_add=[],
            edges_to_add=[_EmittedEdge(_A, _M, "g1"), _EmittedEdge(_B, _M, "g1")],
            node_evidence=[],
            current_turn=6,
        )
        ctr.inc.assert_called_once()
    # ...and the grant is real: the hold is gone.
    _recompute_cause_state_from_chain(case)
    assert mece_contested_root_ids(case) == set()


def test_a_conjunction_modelled_up_front_is_not_counted():
    """The counter must measure the SEQUENCE, not conjunctions. A cause modelled
    as an AND-set when its nodes are emitted — the shape the prompt asks for —
    has CANDIDATE members at edge time and never reaches the observer, so the
    signal stays readable."""
    from unittest.mock import patch

    from faultmaven.core.investigation import causal_graph

    with patch.object(causal_graph, "causal_and_set_late_grouping_total") as ctr:
        case, _d = _conjunction_case()  # grouped at construction
        _recompute_cause_state_from_chain(case)
        ctr.inc.assert_not_called()

    assert case.root_cause_conclusion.contributing_factors == [_LIMIT]


def test_a_late_grouping_of_unvalidated_causes_is_not_counted():
    """Nothing was dissolved: an AND-set completed over causes that had not
    validated grants no identification, so it is not the audited population."""
    from unittest.mock import patch

    from faultmaven.core.investigation import causal_graph

    case, _d = _conjunction_case(and_group=None, second_supports=("b1",))
    case.causal_nodes[_A].evidence_links = []  # neither cause is established
    _recompute_cause_state_from_chain(case)
    assert case.causal_nodes[_A].node_state != NodeState.VALIDATED

    with patch.object(causal_graph, "causal_and_set_late_grouping_total") as ctr:
        ingest_emitted_chain(
            case,
            nodes_to_add=[],
            edges_to_add=[_EmittedEdge(_A, _M, "g1"), _EmittedEdge(_B, _M, "g1")],
            node_evidence=[],
            current_turn=6,
        )
        ctr.inc.assert_not_called()


def test_a_refused_regroup_leaves_a_witness():
    """The refusal never reaches the transcript (ingest is pure and has no
    system_feedback channel), so the model goes on reasoning over a grouping the
    graph does not have. That divergence has to be visible somewhere."""
    from unittest.mock import call, patch

    from faultmaven.core.investigation import causal_graph

    case, _d = _conjunction_case()  # standing group "g1"
    with patch.object(causal_graph, "causal_and_group_regroup_refused_total") as ctr:
        ingest_emitted_chain(
            case,
            nodes_to_add=[],
            edges_to_add=[_EmittedEdge(_A, _M, "g2"), _EmittedEdge(_B, _M, None)],
            node_evidence=[],
            current_turn=6,
        )
        # Both refusal shapes are distinguishable in the metric.
        assert ctr.labels.call_args_list == [
            call(attempt="regroup"),
            call(attempt="ungroup"),
        ]
    # The refusal stands — the graph is unchanged.
    assert incoming_and_groups(_M, case.causal_edges) == {"g1": [_A, _B]}


def test_a_numeric_and_group_is_honored_and_a_boolean_is_not():
    """``and_group: 1`` is plausible JSON from a model numbering its groups, and
    the key is an opaque identity token — discarding it loses a conjunction and
    gains nothing. ``and_group: true`` is the field mistaken for a flag, and
    honoring it would group everything that made the same mistake."""
    from faultmaven.core.investigation.causal_graph import _normalize_and_group

    assert _normalize_and_group(1) == "1"
    assert _normalize_and_group(2) != _normalize_and_group(1)
    assert _normalize_and_group(True) is None
    assert _normalize_and_group(["g1"]) is None
    assert _normalize_and_group({"k": "v"}) is None
