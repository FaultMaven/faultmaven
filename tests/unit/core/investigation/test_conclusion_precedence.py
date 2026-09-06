"""§7.7 precedence: a validated chain root outranks an LLM-authored conclusion.

The conclusion the engine surfaces is the chain-derived mirror whenever a
standing validated, uncontested root exists — so the surfaced text is rendered
from what the chain proves and is structurally incapable of exceeding it. The
LLM-authored conclusion is the explicit fallback, surfaced only when no such root
stands.

These are PROPERTY tests, not instances: the LLM-side conclusion is swept across
everything a model can vary (stated confidence including a VERIFIED over-claim,
linked / unlinked / mis-linked cause, `names_root_node_id` named / absent /
stale), and every assertion is a mechanical read of engine state. Nothing here
depends on model behavior or wording.
"""

import hashlib
import itertools
import logging
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph import (
    _attach_engine_refutation,
    seed_problem_node,
)
from faultmaven.core.investigation.cause_assurance import ENGINE_RCC_AUTHOR
from faultmaven.core.investigation.milestone_engine import (
    _recompute_assessment_state,
    _recompute_cause_state_from_chain,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    CauseAssuranceGrade,
    CauseState,
    ConfidenceLevel,
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
    RootCauseConclusion,
    ValidationMethod,
)

pytestmark = pytest.mark.unit

_ROOT_STATEMENT = (
    "the deploy removed the connection release call so pool connections leak"
)
_RIVAL_STATEMENT = "transient traffic spike exhausted available database connections"
_LLM_TEXT = "the LLM's own worded conclusion, in its own namespace"


# ---------------------------------------------------------------------------
# Fixtures — hand-built graphs, no LLM, no I/O
# ---------------------------------------------------------------------------


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(label, category=EvidenceCategory.CAUSAL_EVIDENCE) -> Evidence:
    # The label is embedded as content tokens so two rows read as INDEPENDENT
    # observations under the INV-29 mirror collapse.
    return Evidence(
        evidence_id=_eid(label),
        summary=f"fact-{label} metric-{label} reading-{label}",
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _node(
    node_id, statement, node_type=NodeType.ROOT, *, support_labels=()
) -> CausalNode:
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
            for label in support_labels
        ],
        generated_at_turn=1,
    )


def _hyp(root_node_id, statement, *, hypothesis_id, path=None) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement,
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root_node_id,
        path=list(path or []),
        generated_at_turn=1,
    )


def _case(nodes=(), edges=(), evidence=(), hyps=()) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        enterprise_id="o",
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
    case.causal_nodes = {n.node_id: n for n in nodes}
    case.causal_edges = list(edges)
    case.evidence = list(evidence)
    case.hypotheses = {h.hypothesis_id: h for h in hyps}
    # The cause-identification anchor; these fixtures model an investigation that
    # has already verified its symptom, so the tests exercise precedence rather
    # than the anchor.
    case.progress.symptom_verified = True
    return case


def _validated_root_case():
    """One standing hypothesis whose chain ROOT validates on two independent
    causal observations (the INV-29 bar) — uncontested."""
    root = _node("cn_00000000000a", _ROOT_STATEMENT, support_labels=["ev_a1", "ev_a2"])
    case = _case(nodes=[root], evidence=[_evidence("ev_a1"), _evidence("ev_a2")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=d.node_id)
    ]
    hyp = _hyp(root.node_id, _ROOT_STATEMENT, hypothesis_id="hyp_0000000000aa")
    case.hypotheses = {hyp.hypothesis_id: hyp}
    # A second STANDING cause with no chain of its own: a link target that cannot
    # itself contest (a flat hypothesis has no validated root).
    rival = _hyp(None, _RIVAL_STATEMENT, hypothesis_id="hyp_0000000000bb")
    case.hypotheses[rival.hypothesis_id] = rival
    return case, root, hyp, rival


def _contested_case():
    """Two DISTINCT simultaneously-validated roots — a §7.1.2 MECE contest."""
    a = _node("cn_00000000000a", _ROOT_STATEMENT, support_labels=["ev_a1", "ev_a2"])
    b = _node("cn_00000000000b", _RIVAL_STATEMENT, support_labels=["ev_b1", "ev_b2"])
    case = _case(
        nodes=[a, b],
        evidence=[_evidence(x) for x in ("ev_a1", "ev_a2", "ev_b1", "ev_b2")],
    )
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=a.node_id, effect_node_id=d.node_id),
        CausalEdge(cause_node_id=b.node_id, effect_node_id=d.node_id),
    ]
    case.hypotheses = {
        h.hypothesis_id: h
        for h in (
            _hyp(a.node_id, _ROOT_STATEMENT, hypothesis_id="hyp_0000000000aa"),
            _hyp(b.node_id, _RIVAL_STATEMENT, hypothesis_id="hyp_0000000000bb"),
        )
    }
    return case


def _no_root_case():
    """A standing hypothesis with no chain at all — nothing can validate."""
    case = _case()
    hyp = _hyp(None, _ROOT_STATEMENT, hypothesis_id="hyp_0000000000aa")
    case.hypotheses = {hyp.hypothesis_id: hyp}
    return case


def _inconclusive_root_case():
    """A root with a SINGLE causal support: held INCONCLUSIVE by the INV-29
    independent-support bar, so no root stands validated."""
    root = _node("cn_00000000000a", _ROOT_STATEMENT, support_labels=["ev_a1"])
    case = _case(nodes=[root], evidence=[_evidence("ev_a1")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=d.node_id)
    ]
    hyp = _hyp(root.node_id, _ROOT_STATEMENT, hypothesis_id="hyp_0000000000aa")
    case.hypotheses = {hyp.hypothesis_id: hyp}
    return case


def _refuted_root_case():
    """A once-grounded root carrying a durable engine refutation (M6)."""
    case, root, _hyp_a, _rival = _validated_root_case()
    _attach_engine_refutation(
        case,
        root.node_id,
        "fix applied, symptom persists",
        # #987: the durable M6 record states an engine INFERENCE with the case
        # records it was derived from, never a first-person observation.
        "engine inference (M6): a fix recorded as executed at turn 2 did not hold",
    )
    return case


# ---------------------------------------------------------------------------
# The sweep: every LLM-side conclusion shape a model can produce
# ---------------------------------------------------------------------------

# (likelihood, confidence_level) — includes the VERIFIED over-claim, which is the
# shape the precedence most needs to dominate.
_CONFIDENCES = [
    (0.95, ConfidenceLevel.VERIFIED),
    (0.8, ConfidenceLevel.CONFIDENT),
    (0.6, ConfidenceLevel.PROBABLE),
    (0.3, ConfidenceLevel.SPECULATION),
]
# The §7.6 cause link: absent, correct, or pointing at another standing cause.
_LINKS = ["unlinked", "linked_to_root", "linked_elsewhere"]
# The INV-35 attribution hint: absent, naming the validated root, or stale.
_NAMED = ["unnamed", "names_root", "names_unknown"]

_LLM_RCC_SHAPES = list(itertools.product(_CONFIDENCES, _LINKS, _NAMED))


def _llm_rcc(shape, *, root_node_id, hyp_id, rival_hyp_id):
    (likelihood, level), link, named = shape
    return RootCauseConclusion(
        root_cause=_LLM_TEXT,
        mechanism="as the LLM described it, in prose",
        confidence_level=level,
        likelihood=likelihood,
        validated_hypothesis_id={
            "unlinked": None,
            "linked_to_root": hyp_id,
            "linked_elsewhere": rival_hyp_id,
        }[link],
        names_root_node_id={
            "unnamed": None,
            "names_root": root_node_id,
            "names_unknown": "cn_ffffffffffff",
        }[named],
        contributing_factors=["a factor the mirror does not carry"],
        determined_by="agent",
    )


def _shape_id(shape):
    (likelihood, level), link, named = shape
    return f"{level.value}-{link}-{named}"


_SWEEP = pytest.mark.parametrize(
    "shape", _LLM_RCC_SHAPES, ids=[_shape_id(s) for s in _LLM_RCC_SHAPES]
)


def _force_precedence(monkeypatch, enabled: bool) -> None:
    """Pin the precedence flag on the settings object the engine actually reads.

    ``get_settings`` is a module-global singleton another test may rebuild via
    ``reset_settings()``; patching only the instance would leave the code reading
    a different one. Uses the REAL ``FeatureSettings`` field — a stand-in object
    would make a dead gate pass.
    """
    from faultmaven.config import settings as settings_mod

    s = settings_mod.get_settings()
    monkeypatch.setattr(s.features, "chain_authored_conclusion", enabled)
    monkeypatch.setattr(settings_mod, "get_settings", lambda: s)


def _authored_fields(rcc):
    """The conclusion's authored surface — what "stands as written" means. The
    §7.6 cause link is deliberately excluded: linking is an engine write that
    predates this precedence and records only WHICH hypothesis the prose names."""
    return (
        rcc.root_cause,
        rcc.mechanism,
        rcc.confidence_level,
        rcc.likelihood,
        rcc.determined_by,
        tuple(rcc.contributing_factors or ()),
    )


# ---------------------------------------------------------------------------
# Property 1 — with a standing validated uncontested root, the mirror is the
# surfaced conclusion for EVERY LLM-side shape
# ---------------------------------------------------------------------------


@_SWEEP
def test_validated_root_takes_the_conclusion_over_any_llm_shape(shape):
    case, root, hyp, rival = _validated_root_case()
    case.root_cause_conclusion = _llm_rcc(
        shape,
        root_node_id=root.node_id,
        hyp_id=hyp.hypothesis_id,
        rival_hyp_id=rival.hypothesis_id,
    )
    _recompute_cause_state_from_chain(case)

    assert case.progress.cause_state == CauseState.IDENTIFIED
    rcc = case.root_cause_conclusion
    assert rcc.determined_by == ENGINE_RCC_AUTHOR
    assert rcc.root_cause == root.statement
    assert rcc.validated_hypothesis_id == hyp.hypothesis_id
    assert _LLM_TEXT not in rcc.root_cause
    # The confidence is grade-derived, so no stated confidence — including the
    # VERIFIED over-claim — survives onto a merely mechanistic root.
    assert rcc.confidence_level == ConfidenceLevel.CONFIDENT
    assert rcc.likelihood == 0.8


@_SWEEP
def test_flag_off_leaves_every_llm_shape_as_written(monkeypatch, shape):
    """Flag OFF restores the older precedence exactly: the conclusion is the same
    object, with every authored field untouched, even beside a validated root."""
    _force_precedence(monkeypatch, False)
    case, root, hyp, rival = _validated_root_case()
    own = _llm_rcc(
        shape,
        root_node_id=root.node_id,
        hyp_id=hyp.hypothesis_id,
        rival_hyp_id=rival.hypothesis_id,
    )
    before = _authored_fields(own)
    case.root_cause_conclusion = own
    _recompute_cause_state_from_chain(case)

    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.root_cause_conclusion is own
    assert _authored_fields(case.root_cause_conclusion) == before


def test_mirror_renders_mechanism_from_the_chains_rung_statements():
    """The mirror's mechanism is the chain itself, joined rung by rung — the text
    is a render of the graph, not prose carried over from the conclusion.

    The synthetic PROBLEM terminal is NOT part of it (#1097): it is the engine's
    anchor rather than a mechanism step, and the report prints this under "How
    it produced the symptom", so appending it restated the heading in the
    graph's own arrow notation."""
    root = _node("cn_00000000000a", _ROOT_STATEMENT, support_labels=["ev_a1", "ev_a2"])
    rung = _node(
        "cn_00000000000e",
        "requests queue behind the exhausted pool",
        NodeType.INTERMEDIATE,
    )
    case = _case(nodes=[root, rung], evidence=[_evidence("ev_a1"), _evidence("ev_a2")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=rung.node_id),
        CausalEdge(cause_node_id=rung.node_id, effect_node_id=d.node_id),
    ]
    hyp = _hyp(
        root.node_id,
        _ROOT_STATEMENT,
        hypothesis_id="hyp_0000000000aa",
        path=[root.node_id, rung.node_id, d.node_id],
    )
    case.hypotheses = {hyp.hypothesis_id: hyp}
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_LLM_TEXT,
        mechanism="an elegant paragraph the engine does not keep",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.95,
        determined_by="agent",
    )
    _recompute_cause_state_from_chain(case)

    rcc = case.root_cause_conclusion
    assert rcc.determined_by == ENGINE_RCC_AUTHOR
    assert rcc.mechanism == rung.statement


def test_replacement_drops_llm_only_fields_rather_than_blending_them():
    """Single authority: the engine does not carry LLM prose into text it renders
    from the graph, so an LLM-authored `contributing_factors` is dropped on
    replacement rather than blended in.

    The mirror does populate that field — from the graph's M7 AND-sets (#1096,
    `validated_and_conjuncts`) — which is why this fixture's empty result is the
    assertion that matters: its graph carries no conjunction, so the engine has
    nothing of its own to name and the LLM's two factors are simply gone."""
    case, root, hyp, rival = _validated_root_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_LLM_TEXT,
        mechanism="prose",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        contributing_factors=["undersized instance", "no connection ceiling alert"],
        determined_by="agent",
    )
    _recompute_cause_state_from_chain(case)

    assert case.root_cause_conclusion.determined_by == ENGINE_RCC_AUTHOR
    assert case.root_cause_conclusion.contributing_factors == []


# ---------------------------------------------------------------------------
# Property 2 — with no standing validated root, the LLM conclusion IS the
# conclusion and stands exactly as written (the explicit fallback)
# ---------------------------------------------------------------------------


_NO_ROOT_SHAPES = {
    "no_chain": _no_root_case,
    "root_inconclusive": _inconclusive_root_case,
    "root_refuted": _refuted_root_case,
}


@pytest.mark.parametrize(
    "factory", list(_NO_ROOT_SHAPES.values()), ids=list(_NO_ROOT_SHAPES)
)
@_SWEEP
def test_llm_conclusion_stands_when_no_root_stands(factory, shape):
    case = factory()
    own = _llm_rcc(
        shape,
        root_node_id="cn_00000000000a",
        hyp_id="hyp_0000000000aa",
        rival_hyp_id="hyp_0000000000bb",
    )
    before = _authored_fields(own)
    case.root_cause_conclusion = own
    _recompute_cause_state_from_chain(case)

    assert case.progress.cause_state != CauseState.IDENTIFIED
    assert case.root_cause_conclusion is own
    assert _authored_fields(case.root_cause_conclusion) == before


@_SWEEP
def test_contested_identification_asserts_nothing_and_preserves_the_fallback(shape):
    """A MECE contest is not disconfirmation: the engine mints no mirror (naming
    one of several exclusive validated causes would be an arbitrary pick) and the
    LLM's conclusion is read-suppressed, never rewritten."""
    case = _contested_case()
    own = _llm_rcc(
        shape,
        root_node_id="cn_00000000000a",
        hyp_id="hyp_0000000000aa",
        rival_hyp_id="hyp_0000000000bb",
    )
    before = _authored_fields(own)
    case.root_cause_conclusion = own
    _recompute_cause_state_from_chain(case)

    assert case.progress.cause_identification_contested is True
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert case.root_cause_conclusion is own
    assert _authored_fields(case.root_cause_conclusion) == before


def test_overclaim_seam_still_fires_on_a_fallback_conclusion(caplog):
    """The reconciliation layer still governs the fallback lane: a conclusion
    claiming VERIFIED with NO validated root behind it trips the over-claim seam
    at grade NO_ROOT."""
    case = _no_root_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_LLM_TEXT,
        mechanism="prose",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.95,
        determined_by="agent",
    )
    with caplog.at_level(
        logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
    ):
        _recompute_assessment_state(case)

    assert case.progress.cause_assurance == CauseAssuranceGrade.NO_ROOT
    assert case.progress.cause_overclaim is True
    recs = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "cause_confidence_overclaim"
    ]
    assert recs and recs[-1].cause_assurance == "no_root"


def test_replaced_conclusion_is_not_restored_when_the_root_demotes():
    """Replacement is one-way. Once the mirror's root demotes the case asserts
    NOTHING — the engine keeps no copy of the replaced text to restore, and
    re-surfacing it would assert a cause no validated root backs. (The conclusion
    swept here is deliberately divergent from the root statement, so this holds
    regardless of whether the replaced text named the demoted root's cause.)"""
    case, root, hyp, rival = _validated_root_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_LLM_TEXT,
        mechanism="prose",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.95,
        determined_by="agent",
    )
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion.determined_by == ENGINE_RCC_AUTHOR

    case.evidence = []  # the backing observations are gone
    _recompute_cause_state_from_chain(case)

    assert root.node_state != NodeState.VALIDATED
    assert case.root_cause_conclusion is None


# ---------------------------------------------------------------------------
# Property 3 — the counter records replacements, and only replacements
# ---------------------------------------------------------------------------


def _inversions():
    return patch(
        "faultmaven.core.investigation.causal_graph.rcc_precedence_inversion_total"
    )


def test_counter_records_the_replacement_labeled_by_provider():
    case, root, hyp, rival = _validated_root_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_LLM_TEXT,
        mechanism="prose",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        determined_by="agent",
    )
    with _inversions() as counter:
        _recompute_cause_state_from_chain(case)

    assert counter.labels.call_count == 1
    assert set(counter.labels.call_args.kwargs) == {"provider"}
    assert counter.labels.return_value.inc.call_count == 1


def test_counter_ignores_the_first_mint_into_an_absent_conclusion():
    case, root, hyp, rival = _validated_root_case()
    assert case.root_cause_conclusion is None
    with _inversions() as counter:
        _recompute_cause_state_from_chain(case)

    assert case.root_cause_conclusion.determined_by == ENGINE_RCC_AUTHOR
    assert counter.labels.call_count == 0


def test_counter_ignores_a_mirror_refreshing_a_mirror():
    """A stale engine mirror (wrong grade for its root) is re-minted every
    recompute — that is mirror maintenance, not the chain taking a conclusion
    over from the model."""
    case, root, hyp, rival = _validated_root_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=root.statement,
        mechanism="Directly produces the observed problem.",
        confidence_level=ConfidenceLevel.VERIFIED,  # pre-cap over-claim
        likelihood=0.9,
        validated_hypothesis_id=hyp.hypothesis_id,
        determined_by=ENGINE_RCC_AUTHOR,
    )
    with _inversions() as counter:
        _recompute_cause_state_from_chain(case)

    assert case.root_cause_conclusion.confidence_level == ConfidenceLevel.CONFIDENT
    assert counter.labels.call_count == 0


def test_counter_ignores_a_no_op_turn_on_a_faithful_mirror():
    case, root, hyp, rival = _validated_root_case()
    _recompute_cause_state_from_chain(case)  # mints the mirror
    with _inversions() as counter:
        _recompute_cause_state_from_chain(case)

    assert counter.labels.call_count == 0


def test_counter_ignores_a_turn_that_mints_nothing():
    case = _no_root_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_LLM_TEXT,
        mechanism="prose",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        determined_by="agent",
    )
    with _inversions() as counter:
        _recompute_cause_state_from_chain(case)

    assert counter.labels.call_count == 0


def test_counter_stays_flat_while_the_precedence_is_off(monkeypatch):
    _force_precedence(monkeypatch, False)
    case, root, hyp, rival = _validated_root_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_LLM_TEXT,
        mechanism="prose",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        determined_by="agent",
    )
    with _inversions() as counter:
        _recompute_cause_state_from_chain(case)

    assert counter.labels.call_count == 0
