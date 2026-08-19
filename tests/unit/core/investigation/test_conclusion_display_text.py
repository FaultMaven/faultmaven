"""#1097 — the conclusion's user-facing fields carry prose, not engine notation.

Two fields of ``RootCauseConclusion`` are rendered VERBATIM to a user (and, for
the mechanism, into any runbook harvested from the case):

- ``established_by`` held the id-bearing audit line the confirm-stamp writes
  onto its node link — one string was serving an internal audit surface and a
  user-facing one, so ``ev_…``/``cn_…`` and "M2 gone⇒gone" reached the reader.
- ``mechanism`` ended with the graph's synthetic PROBLEM terminal, so the chain
  notation dangled into prose under a heading that already said it.

Both are fixed at the producer, and normalized at the read for the cases that
resolved before the fix — terminal cases never recompute, so their stored rows
keep the internal form forever.
"""

import hashlib
import re
from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.causal_graph import (
    mechanism_for_chain,
    seed_problem_node,
)
from faultmaven.core.investigation.cause_assurance import (
    confirm_root_from_resolution_absence,
)
from faultmaven.core.investigation.milestone_engine import (
    _recompute_cause_state_from_chain,
)
from faultmaven.modules.case.contracts import (
    CONFIRMED_ESTABLISHED_BY,
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
    established_by_for_display,
    mechanism_for_display,
)

pytestmark = pytest.mark.unit

# The exact shapes the issue reported leaking.
_ENGINE_ID = re.compile(r"\b(?:ev|cn)_[0-9a-f]{12}\b")
_MILESTONE_SHORTHAND = re.compile(r"\bM\d\b|gone⇒gone")

_ROOT = "cn_0000000000aa"
_RUNG = "cn_0000000000bb"
_CAUSE_TEXT = "checkout-api v2.14.0 retains an unbounded orderSummaryCache"
_RUNG_TEXT = "JVM heap pressure causes GC pauses and readiness failure before OOM"


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(label: str, category=EvidenceCategory.CAUSAL_EVIDENCE) -> Evidence:
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


def _confirmed_case():
    """A validated one-rung chain (root -> rung -> D) the user has confirmed
    resolved — the shape that mints an ``established_by``."""
    root = CausalNode(
        node_id=_ROOT,
        statement=_CAUSE_TEXT,
        node_type=NodeType.ROOT,
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
            for label in ("a1", "a2")
        ],
        generated_at_turn=1,
    )
    rung = CausalNode(
        node_id=_RUNG,
        statement=_RUNG_TEXT,
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=False,
        generated_at_turn=1,
    )
    case = Case(
        case_id="case_000000000097",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=8,
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
    case.causal_nodes = {n.node_id: n for n in (root, rung)}
    case.evidence = [_evidence("a1"), _evidence("a2")]
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=_ROOT, effect_node_id=_RUNG),
        CausalEdge(cause_node_id=_RUNG, effect_node_id=d.node_id),
    ]
    hyp = Hypothesis(
        hypothesis_id="hyp_0000000000aa",
        statement=_CAUSE_TEXT,
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=_ROOT,
        path=[_ROOT, _RUNG, d.node_id],
        generated_at_turn=1,
    )
    case.hypotheses = {hyp.hypothesis_id: hyp}
    case.progress.symptom_verified = True
    _recompute_cause_state_from_chain(case)
    case.evidence.append(_evidence("absence", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE))
    case.evidence[-1].collected_at_turn = 8
    return case, hyp


# ---------------------------------------------------------------------------
# The producers
# ---------------------------------------------------------------------------


def test_the_minted_provenance_is_prose_and_the_audit_line_keeps_its_ids():
    """One string cannot serve both surfaces. The node link is the durable audit
    record — the ids are the point of it — while the conclusion is rendered to a
    reader verbatim."""
    case, _hyp = _confirmed_case()

    assert confirm_root_from_resolution_absence(case) is True

    established_by = case.root_cause_conclusion.established_by
    assert established_by == CONFIRMED_ESTABLISHED_BY
    assert not _ENGINE_ID.search(established_by)
    assert not _MILESTONE_SHORTHAND.search(established_by)

    # The audit form survives, unchanged, where it belongs.
    audit = case.causal_nodes[_ROOT].evidence_links[-1].reasoning
    assert _ENGINE_ID.search(audit)
    assert "gone⇒gone" in audit


def test_the_mechanism_does_not_dangle_the_problem_node():
    """The PROBLEM node is the engine's synthetic anchor, not a mechanism step,
    and the report's heading already says what the chain arrives at."""
    case, _hyp = _confirmed_case()
    confirm_root_from_resolution_absence(case)

    mechanism = case.root_cause_conclusion.mechanism
    assert mechanism == _RUNG_TEXT
    assert "the problem" not in mechanism


def test_the_arrows_between_real_rungs_survive():
    """Only the synthetic terminal goes — the arrows between rungs ARE the
    chain, and dropping them would lose the mechanism."""
    case, hyp = _confirmed_case()
    third = CausalNode(
        node_id="cn_0000000000cc",
        statement="the container is OOM-killed",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=False,
        generated_at_turn=1,
    )
    case.causal_nodes[third.node_id] = third
    d_id = hyp.path[-1]
    hyp.path = [_ROOT, _RUNG, third.node_id, d_id]

    mechanism = mechanism_for_chain(case, hyp)
    assert mechanism == f"{_RUNG_TEXT} → the container is OOM-killed"


def test_a_root_that_directly_produces_the_problem_states_a_sentence():
    """The no-rung case always read as a sentence; the rung case now agrees with
    it instead of emitting arrow notation."""
    case, hyp = _confirmed_case()
    hyp.path = [_ROOT, hyp.path[-1]]

    assert mechanism_for_chain(case, hyp) == "Directly produces the observed problem."


def test_both_mint_sites_build_the_same_mechanism():
    """The per-turn mirror and the terminal confirm-stamp render one conclusion.
    They had a copy each of this rule, on the field a reader sees."""
    from faultmaven.core.investigation import cause_assurance

    case, hyp = _confirmed_case()
    # Per-turn mirror (already minted by the recompute in the fixture).
    per_turn = case.root_cause_conclusion.mechanism
    # Terminal stamp re-mints over the same chain.
    confirm_root_from_resolution_absence(case)

    assert case.root_cause_conclusion.mechanism == per_turn
    assert cause_assurance._graph_hooks()["mechanism"] is mechanism_for_chain


# ---------------------------------------------------------------------------
# The read-side normalization, for rows minted before the fix
# ---------------------------------------------------------------------------


def test_a_legacy_audit_provenance_is_replaced_with_its_prose_form():
    """Terminal cases never recompute, so the reported case keeps the leaked
    string in storage. The confirm-stamp was the field's ONLY writer, so an
    id-bearing value is that provenance exactly — restate it, don't drop it."""
    legacy = (
        "engine: user-confirmed resolution at turn 8 — causal-absence "
        "ev_a9f662e1c86f bears on root cn_984e2337cbda (M2 gone⇒gone)"
    )

    assert established_by_for_display(legacy) == CONFIRMED_ESTABLISHED_BY


def test_prose_and_empty_provenance_pass_through_untouched():
    assert established_by_for_display(CONFIRMED_ESTABLISHED_BY) == (
        CONFIRMED_ESTABLISHED_BY
    )
    assert established_by_for_display(None) is None
    assert established_by_for_display("") == ""
    # An id-shaped word that is not an engine id must not trip it.
    assert established_by_for_display("the ev_notanid row") == "the ev_notanid row"


def test_a_legacy_mechanism_loses_only_its_dangling_terminal():
    assert mechanism_for_display("A → B → the problem") == "A → B"
    assert mechanism_for_display("A → B") == "A → B"
    assert mechanism_for_display("Directly produces the observed problem.") == (
        "Directly produces the observed problem."
    )
    assert mechanism_for_display(None) is None
