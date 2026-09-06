"""§7.6 / INV-34 (#656) — the LLM-authored RootCauseConclusion lifecycle.

An LLM conclusion arrives as free text with no cause link, so the retraction
machinery (``retract_disconfirmed_rcc``, the M6 demotion) could not reach it and
the terminal readers trusted it even through a MECE contest. This pins the three
disciplines that close that gap WITHOUT the engine ever authoring an LLM
conclusion:

  * **link** — ``link_llm_rcc_to_cause`` conservatively attributes the LLM's
    stated cause to a standing hypothesis (single unambiguous STRONG match), the
    link that lets disconfirmation retraction reach it;
  * **retract on disconfirmation** — a linked LLM conclusion whose cause is
    disconfirmed is cleared at source, like an engine one (NO-INCORRECT-
    CONCLUSION), while an unlinked one stays the documented residual;
  * **refresh** — the M6 blanket clear no longer wipes a conclusion RE-GROUNDED
    onto a different, still-standing cause (NO-COLLAPSE);
  * **contest read-suppress** — while identification is MECE-contested, an LLM
    conclusion does not count as identified (reversible, no mutation).
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph import (
    _attach_engine_refutation,
    demote_disconfirmed_cause_via_evidence,
    link_llm_rcc_to_cause,
    retract_disconfirmed_rcc,
    seed_problem_node,
)
from faultmaven.core.investigation.milestone_engine import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.terminal_transitions import _cause_identified
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    RootCauseConclusion,
    ValidationMethod,
    WorkingConclusion,
)

pytestmark = pytest.mark.unit

_ENGINE_RCC_AUTHOR = "engine:chain_validation"


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(label, category=EvidenceCategory.CAUSAL_EVIDENCE) -> Evidence:
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


def _root(node_id, statement, *, support_labels=()) -> CausalNode:
    links = [
        NodeEvidenceLink(
            evidence_id=_eid(label),
            stance=EvidenceStance.SUPPORTS,
            reasoning="bears on the root",
            linked_at_turn=2,
        )
        for label in support_labels
    ]
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=links,
        generated_at_turn=1,
    )


def _hyp(
    root_node_id,
    statement,
    *,
    hypothesis_id,
    state=HypothesisState.ACTIVE,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement,
        category=HypothesisCategory.DATABASE,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root_node_id,
        generated_at_turn=1,
        refutation_reason=("disproven" if state == HypothesisState.REFUTED else None),
    )


def _case(nodes=None, edges=None, evidence=None, hyps=None) -> Case:
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
    case.causal_nodes = {n.node_id: n for n in (nodes or [])}
    case.causal_edges = edges or []
    case.evidence = evidence or []
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    case.progress.symptom_verified = True
    return case


def _llm_rcc(root_cause, *, vhid=None, likelihood=0.85) -> RootCauseConclusion:
    return RootCauseConclusion(
        root_cause=root_cause,
        mechanism="how it produced the symptom",
        likelihood=likelihood,
        confidence_level=ConfidenceLevel.from_score(likelihood),
        validated_hypothesis_id=vhid,
        determined_by="agent",  # the LLM's own stance
    )


_POOL_LEAK = (
    "the deploy removed the connection release call so pool connections "
    "leak until exhaustion"
)
_TRAFFIC_SPIKE = (
    "a transient traffic spike briefly exhausted available database connections"
)


# ---------------------------------------------------------------------------
# link_llm_rcc_to_cause — conservative single-match attribution
# ---------------------------------------------------------------------------


def test_link_single_strong_match():
    hyp = _hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK)
    with patch(
        "faultmaven.core.investigation.causal_graph.llm_rcc_cause_linked_total"
    ) as counter:
        assert link_llm_rcc_to_cause(case) is True
    assert case.root_cause_conclusion.validated_hypothesis_id == hyp.hypothesis_id
    assert counter.inc.call_count == 1


def test_no_link_when_two_contenders_ambiguous():
    """Two standing hypotheses both >= AMBIGUOUS against the conclusion → don't
    guess (the orphan-chain T1 discipline)."""
    a = _hyp(
        "cn_a",
        "database connection pool connections exhausted on deploy",
        hypothesis_id="hyp_0000000000aa",
    )
    b = _hyp(
        "cn_b",
        "database connection pool exhausted under checkout load",
        hypothesis_id="hyp_0000000000bb",
    )
    case = _case(hyps=[a, b])
    case.root_cause_conclusion = _llm_rcc(
        "database connection pool connections exhausted"
    )
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_no_link_when_only_ambiguous_match():
    """A sole match below STRONG (only AMBIGUOUS) is not linked."""
    hyp = _hyp(
        "cn_a",
        "database connection pool exhausted on the recent deploy",
        hypothesis_id="hyp_0000000000aa",
    )
    case = _case(hyps=[hyp])
    # Shares only a couple of tokens — AMBIGUOUS band, below STRONG.
    case.root_cause_conclusion = _llm_rcc("the deploy introduced a regression")
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_no_link_on_single_coincidental_word():
    """Containment on one shared word must not link (the substantive-overlap
    floor — the same guard as the orphan T1 re-attach)."""
    hyp = _hyp(
        "cn_a", "database connection pool exhausted", hypothesis_id="hyp_0000000000aa"
    )
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc("database")  # 1 token, contained
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_engine_rcc_never_linked():
    hyp = _hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")
    case = _case(hyps=[hyp])
    rcc = _llm_rcc(_POOL_LEAK)
    rcc.determined_by = _ENGINE_RCC_AUTHOR
    case.root_cause_conclusion = rcc
    assert link_llm_rcc_to_cause(case) is False


def test_already_linked_to_live_is_stable():
    hyp = _hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")
    other = _hyp("cn_b", _TRAFFIC_SPIKE, hypothesis_id="hyp_0000000000bb")
    case = _case(hyps=[hyp, other])
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK, vhid=other.hypothesis_id)
    # Points at a LIVE hypothesis (even if not the best text match) — left as-is.
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id == other.hypothesis_id


def test_dangling_link_is_re_resolved():
    hyp = _hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK, vhid="hyp_gone")
    assert link_llm_rcc_to_cause(case) is True
    assert case.root_cause_conclusion.validated_hypothesis_id == hyp.hypothesis_id


def test_dangling_link_cleared_when_no_rematch():
    """A dangling link with no clean re-match is cleared (not left pointing at a
    removed hypothesis) so _representative_cause_hypothesis falls back to the
    max-likelihood proxy instead of returning None and disabling M6."""
    # Sole hypothesis is unrelated → no clean match.
    hyp = _hyp("cn_a", _TRAFFIC_SPIKE, hypothesis_id="hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(
        "an entirely unrelated storage layer fault", vhid="hyp_gone"
    )
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_no_rcc_is_noop():
    case = _case(hyps=[_hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")])
    assert link_llm_rcc_to_cause(case) is False


# ---------------------------------------------------------------------------
# retract_disconfirmed_rcc — a LINKED LLM conclusion is retracted at source
# (the gap M6 misses when cause_state never reached IDENTIFIED)
# ---------------------------------------------------------------------------


def test_linked_llm_rcc_retracted_when_cause_refuted():
    hyp = _hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK, vhid=hyp.hypothesis_id)
    hyp.state = HypothesisState.REFUTED
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "llm_rcc_retracted_disconfirmed_total"
    ) as counter:
        assert retract_disconfirmed_rcc(case) is True
    assert case.root_cause_conclusion is None
    assert counter.inc.call_count == 1


def test_unlinked_llm_rcc_survives_refutation_documented_residual():
    """An unlinkable free-text conclusion has no cause link, so link-based
    retraction cannot attribute it — it stays (the documented residual, unchanged
    from the pre-INV-34 behavior; no regression). NO-COLLAPSE: never guess a link
    to erase it."""
    hyp = _hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc("something the engine cannot map")
    hyp.state = HypothesisState.REFUTED
    assert retract_disconfirmed_rcc(case) is False
    assert case.root_cause_conclusion is not None


# ---------------------------------------------------------------------------
# M6 end-to-end: the link redirects the demotion trigger to the LLM's named
# cause, so a re-grounded conclusion survives while a same-cause one is cleared
# ---------------------------------------------------------------------------


def _record_failed_fix(case, *, fix_turn=2, persist_turn=3):
    """Record the FAILED TREATMENT these M6 tests narrate but never persisted.

    Since #987 M6 establishes its preconditions rather than inferring them: an
    accepted actionable ProposedAction (the user executed a fix) plus a
    SYMPTOM_EVIDENCE row at/after it (the problem observed still present).
    "fix applied, symptom persists" living only in a reasoning string is the
    gap that let M6 mint that sentence as a fact on a SUCCESSFULLY fixed case.
    """
    from faultmaven.modules.case.contracts import (
        InvestigationActionType,
        ProposedAction,
    )

    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="apply the fix",
            proposed_in_turn=fix_turn,
            state="accepted",
        )
    )
    row = _evidence("still_failing", EvidenceCategory.SYMPTOM_EVIDENCE)
    row.collected_at_turn = persist_turn
    case.evidence.append(row)
    return case


def _identified_case():
    """A case grounded to IDENTIFIED via a validated root (X)."""
    root = _root("cn_0000000000a1", _POOL_LEAK, support_labels=["s1", "s2"])
    case = _case(nodes=[root], evidence=[_evidence("s1"), _evidence("s2")])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=d.node_id)
    ]
    hyp_x = _hyp(root.node_id, _POOL_LEAK, hypothesis_id="hyp_0000000000aa")
    case.hypotheses = {hyp_x.hypothesis_id: hyp_x}
    _recompute_cause_state_from_chain(case)
    return case, root, hyp_x


def test_recompute_keeps_regrounded_conclusion_on_other_standing_cause():
    """The routed residual: the M6 demotion of X must NOT wipe a conclusion the
    LLM re-grounded onto a DIFFERENT still-standing cause Y. The link (run first
    each recompute) points the demotion trigger at Y — X's own root refutation
    demotes X via derive, and the RCC(Y) survives the full recompute."""
    case, root_x, hyp_x = _identified_case()
    _record_failed_fix(case)
    # A second standing cause Y the LLM has now concluded on (flat — no node).
    hyp_y = _hyp(None, _TRAFFIC_SPIKE, hypothesis_id="hyp_0000000000bb")
    case.hypotheses[hyp_y.hypothesis_id] = hyp_y
    case.root_cause_conclusion = _llm_rcc(
        _TRAFFIC_SPIKE
    )  # unlinked; recompute links it
    # X's fix failed — a counterfactual absence refute lands on X's root.
    _attach_engine_refutation(
        case,
        root_x.node_id,
        "fix applied, symptom persists",
        "engine inference (M6): a fix recorded as executed at turn 2 did not hold",
    )

    _recompute_cause_state_from_chain(case)

    assert root_x.node_state == NodeState.REFUTED  # X demoted by its own evidence
    # ...but the re-grounded conclusion on Y survives, now linked to Y.
    assert case.root_cause_conclusion is not None
    assert case.root_cause_conclusion.root_cause == _TRAFFIC_SPIKE
    assert case.root_cause_conclusion.validated_hypothesis_id == hyp_y.hypothesis_id


def test_m6_clears_conclusion_naming_the_disconfirmed_cause():
    case, root_x, hyp_x = _identified_case()
    _record_failed_fix(case)
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK, vhid=hyp_x.hypothesis_id)
    hyp_x.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=hyp_x.hypothesis_id,
            evidence_id=_eid("fail"),
            stance=EvidenceStance.REFUTES,
            reasoning="fix applied, symptom persists",
            stance_confidence=0.9,
        )
    )
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "llm_rcc_retracted_disconfirmed_total"
    ) as counter:
        assert demote_disconfirmed_cause_via_evidence(case) is True
    assert case.root_cause_conclusion is None
    assert counter.inc.call_count == 1


def test_m6_clears_unlinked_conclusion_on_sole_disconfirmed_cause():
    """When the sole standing cause is disconfirmed and the conclusion is
    unlinkable, M6's max-likelihood proxy resolves to it and clears it (safe
    blanket — NO-INCORRECT-CONCLUSION over a preserved guess)."""
    case, root_x, hyp_x = _identified_case()
    _record_failed_fix(case)
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK)  # no vhid
    hyp_x.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=hyp_x.hypothesis_id,
            evidence_id=_eid("fail"),
            stance=EvidenceStance.REFUTES,
            reasoning="fix applied, symptom persists",
            stance_confidence=0.9,
        )
    )
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "llm_rcc_retracted_disconfirmed_total"
    ) as counter:
        assert demote_disconfirmed_cause_via_evidence(case) is True
    assert case.root_cause_conclusion is None
    # The conclusion was NOT linked to the disconfirmed cause (the max-likelihood
    # proxy resolved to it), so this collateral wipe is not a "named cause
    # disconfirmed" event and must not inflate the failed-fix signal.
    assert counter.inc.call_count == 0


def test_m6_proxy_wipe_of_conclusion_linked_elsewhere_not_counted():
    """A conclusion linked to a DIFFERENT standing cause is protected from M6 (the
    trigger points at the linked cause, not the proxy) — so it is never wiped and
    never counted. Pins that the counter tracks only genuine named-cause
    disconfirmations, not proxy collateral."""
    case, root_x, hyp_x = _identified_case()
    hyp_y = _hyp(None, _TRAFFIC_SPIKE, hypothesis_id="hyp_0000000000bb")
    case.hypotheses[hyp_y.hypothesis_id] = hyp_y
    case.root_cause_conclusion = _llm_rcc(_TRAFFIC_SPIKE, vhid=hyp_y.hypothesis_id)
    hyp_x.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=hyp_x.hypothesis_id,
            evidence_id=_eid("fail"),
            stance=EvidenceStance.REFUTES,
            reasoning="fix applied, symptom persists",
            stance_confidence=0.9,
        )
    )
    with patch(
        "faultmaven.core.investigation.causal_graph."
        "llm_rcc_retracted_disconfirmed_total"
    ) as counter:
        # Representative resolves to the linked hyp_y (not disconfirmed), so M6
        # does not fire at all — the conclusion on Y survives.
        assert demote_disconfirmed_cause_via_evidence(case) is False
    assert case.root_cause_conclusion is not None
    assert counter.inc.call_count == 0


# ---------------------------------------------------------------------------
# Contest read-suppress in _cause_identified (Mechanism 2)
# ---------------------------------------------------------------------------


def test_contest_suppresses_llm_rcc_from_cause_identified():
    case = _case(hyps=[_hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")])
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK)
    case.progress.cause_identification_contested = True
    assert _cause_identified(case) is False


def test_llm_rcc_counts_when_not_contested():
    case = _case(hyps=[_hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")])
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK)
    case.progress.cause_identification_contested = False
    assert _cause_identified(case) is True


def test_contest_suppresses_working_conclusion_too():
    case = _case()
    case.working_conclusion = WorkingConclusion(
        statement=_POOL_LEAK, likelihood=0.9, reasoning="max-likelihood pick"
    )
    case.progress.cause_identification_contested = True
    assert _cause_identified(case) is False


def test_contest_read_suppress_is_reversible():
    """Read-suppression, not retraction: the conclusion is preserved and counts
    again once the contest resolves — no mutation on the suppression path."""
    case = _case(hyps=[_hyp("cn_a", _POOL_LEAK, hypothesis_id="hyp_0000000000aa")])
    case.root_cause_conclusion = _llm_rcc(_POOL_LEAK)
    case.progress.cause_identification_contested = True
    assert _cause_identified(case) is False
    assert case.root_cause_conclusion is not None  # preserved, not erased
    case.progress.cause_identification_contested = False
    assert _cause_identified(case) is True
