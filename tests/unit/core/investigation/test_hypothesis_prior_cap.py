"""A freshly-created hypothesis is a PRIOR, never a
conclusion. ``create_hypothesis`` caps ``initial_likelihood`` strictly below the
SSOT ``CAUSE_IDENTIFIED_LIKELIHOOD`` gate, so no single emission — an over-confident
LLM ``likelihood: 1.0`` especially — can arrive near-conclusion on creation.
"""

import pytest

from faultmaven.core.investigation.hypothesis_manager import (
    NEW_HYPOTHESIS_MAX_PRIOR,
    HypothesisManager,
)
from faultmaven.core.investigation.terminal_transitions import (
    CAUSE_IDENTIFIED_LIKELIHOOD,
)
from faultmaven.modules.case.contracts import HypothesisCategory

pytestmark = pytest.mark.unit


def _mk(initial_likelihood: float):
    return HypothesisManager().create_hypothesis(
        statement="connection pool exhausted",
        category=HypothesisCategory.DATABASE.value,
        initial_likelihood=initial_likelihood,
        current_turn=1,
    )


def test_cap_is_below_the_identified_gate_ssot():
    """The cap is pinned below the single SSOT gate — the invariant that keeps a
    prior from ever tripping IDENTIFIED. (The import-time assert in
    hypothesis_manager enforces this at boot; this test documents it.)"""
    assert NEW_HYPOTHESIS_MAX_PRIOR < CAUSE_IDENTIFIED_LIKELIHOOD


def test_overconfident_llm_prior_is_capped():
    """The headline case: an LLM emits likelihood=1.0; the stored hypothesis
    is capped at the prior bound, not stored near-conclusion."""
    h = _mk(1.0)
    assert h.likelihood == NEW_HYPOTHESIS_MAX_PRIOR
    # initial_likelihood is also capped — it is the baseline for the evidence
    # climb (update_likelihood_from_evidence starts from it), so the climb must
    # start from the capped prior, not the raw 1.0.
    assert h.initial_likelihood == NEW_HYPOTHESIS_MAX_PRIOR


def test_value_below_cap_passes_through_unchanged():
    """The cap is a ceiling, not a rewrite — a modest prior is preserved (the
    runbook matcher's pre-capped <=0.5 values pass through untouched)."""
    h = _mk(0.3)
    assert h.likelihood == 0.3
    assert h.initial_likelihood == 0.3


def test_value_at_cap_is_preserved():
    h = _mk(NEW_HYPOTHESIS_MAX_PRIOR)
    assert h.likelihood == NEW_HYPOTHESIS_MAX_PRIOR


def test_negative_prior_floored_to_zero():
    """Defensive: a nonsensical negative prior floors to 0.0 (never negative)."""
    h = _mk(-0.5)
    assert h.likelihood == 0.0


# ---------------------------------------------------------------------------
# INV-29 companion (#573 B1): the SAME prior discipline on the UPDATE path —
# a direct likelihood update on an evidence-free hypothesis is capped, so the
# LLM cannot fiat belief past the gate the creation cap protects.
# ---------------------------------------------------------------------------


def _linked(h, *, supports=True):
    from faultmaven.modules.case.contracts import (
        EvidenceStance,
        HypothesisEvidenceLink,
    )

    h.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=h.hypothesis_id,
            evidence_id="ev_" + "0" * 12,
            stance=EvidenceStance.SUPPORTS if supports else EvidenceStance.REFUTES,
            reasoning="linked",
            stance_confidence=0.9,
        )
    )
    return h


def test_evidence_free_likelihood_update_is_capped():
    """The headline B1 mechanic: LLM sets likelihood=0.9 by fiat on a
    hypothesis with zero evidence links — capped at the prior bound."""
    m = HypothesisManager()
    h = _mk(0.4)
    m.update_hypothesis_likelihood(h, 0.9, current_turn=3, reason="LLM update")
    assert h.likelihood == NEW_HYPOTHESIS_MAX_PRIOR


def test_supported_hypothesis_update_is_not_capped():
    """With a supporting evidence link the update applies — evidence lifts
    belief past the prior cap."""
    m = HypothesisManager()
    h = _linked(_mk(0.4))
    m.update_hypothesis_likelihood(h, 0.9, current_turn=3, reason="LLM update")
    assert h.likelihood == 0.9


def test_refuting_only_links_do_not_lift_the_cap():
    """Disconfirmation is not grounds for MORE belief: refuting-only links
    leave the hypothesis a prior."""
    m = HypothesisManager()
    h = _linked(_mk(0.4), supports=False)
    m.update_hypothesis_likelihood(h, 0.9, current_turn=3, reason="LLM update")
    assert h.likelihood == NEW_HYPOTHESIS_MAX_PRIOR


def test_capped_rerequest_does_not_reset_stagnation():
    """Progress is judged on the APPLIED value: re-asserting the same over-cap
    number on a capped hypothesis is a no-op and must not reset the
    stagnation/decay counters."""
    m = HypothesisManager()
    h = _mk(NEW_HYPOTHESIS_MAX_PRIOR)
    before = h.iterations_without_progress
    m.update_hypothesis_likelihood(h, 0.95, current_turn=3, reason="LLM update")
    assert h.likelihood == NEW_HYPOTHESIS_MAX_PRIOR
    assert h.iterations_without_progress == before + 1
    assert h.last_progress_at_turn != 3


def test_evidence_free_downward_update_applies():
    """The cap is a ceiling only — lowering belief without evidence is honest
    and passes through."""
    m = HypothesisManager()
    h = _mk(0.4)
    m.update_hypothesis_likelihood(h, 0.2, current_turn=3, reason="LLM update")
    assert h.likelihood == 0.2


def test_hedged_link_does_not_lift_the_cap():
    """A SUPPORTS link the model itself hedges (below
    CAUSAL_STANCE_CONFIDENCE_MIN) is not grounds for MORE belief — the cap
    holds (same bar as the §7.1 chain tally, one shared constant)."""
    from faultmaven.modules.case.contracts import (
        EvidenceStance,
        HypothesisEvidenceLink,
    )

    m = HypothesisManager()
    h = _mk(0.4)
    h.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=h.hypothesis_id,
            evidence_id="ev_" + "0" * 12,
            stance=EvidenceStance.SUPPORTS,
            reasoning="linked but hedged",
            stance_confidence=0.4,
        )
    )
    m.update_hypothesis_likelihood(h, 0.9, current_turn=3, reason="LLM update")
    assert h.likelihood == NEW_HYPOTHESIS_MAX_PRIOR


def test_cap_event_increments_counter():
    from unittest.mock import patch

    m = HypothesisManager()
    h = _mk(0.4)
    with patch(
        "faultmaven.core.investigation.hypothesis_manager."
        "hypothesis_likelihood_capped_no_evidence_total"
    ) as counter:
        m.update_hypothesis_likelihood(h, 0.9, current_turn=3, reason="LLM update")
    assert counter.inc.call_count == 1


def test_cap_never_demotes_below_earned_value():
    """The cap is a ceiling on RAISES only: a hypothesis legitimately above
    the prior bar (belief earned via the evidence formula, which counts links
    the qualifying test may reject as hedged) keeps its earned value — the
    review found the old form forcing 0.95 down to 0.5 and resetting the
    stagnation counters as 'progress'."""
    from faultmaven.modules.case.contracts import (
        EvidenceStance,
        HypothesisEvidenceLink,
    )

    m = HypothesisManager()
    h = _mk(0.5)
    for i in range(3):  # three HEDGED supports: formula counts them
        h.evidence_links.append(
            HypothesisEvidenceLink(
                hypothesis_id=h.hypothesis_id,
                evidence_id="ev_" + f"{i:012d}",
                stance=EvidenceStance.SUPPORTS,
                reasoning="hedged",
                stance_confidence=0.5,
            )
        )
    m.update_likelihood_from_evidence(h, turn=2)
    assert h.likelihood == 0.95  # earned via the formula
    before_iters = h.iterations_without_progress
    m.update_hypothesis_likelihood(h, 0.99, current_turn=3, reason="LLM update")
    # Raise above current refused (no confident link), but never demoted:
    assert h.likelihood == 0.95
    assert h.iterations_without_progress == before_iters + 1  # no fake progress


def test_cap_allows_honest_downward_update_from_earned_value():
    from faultmaven.modules.case.contracts import (
        EvidenceStance,
        HypothesisEvidenceLink,
    )

    m = HypothesisManager()
    h = _mk(0.5)
    for i in range(3):
        h.evidence_links.append(
            HypothesisEvidenceLink(
                hypothesis_id=h.hypothesis_id,
                evidence_id="ev_" + f"{i:012d}",
                stance=EvidenceStance.SUPPORTS,
                reasoning="hedged",
                stance_confidence=0.5,
            )
        )
    m.update_likelihood_from_evidence(h, turn=2)
    m.update_hypothesis_likelihood(h, 0.6, current_turn=3, reason="LLM update")
    assert h.likelihood == 0.6  # downward moves always pass


def test_chain_root_grounding_lifts_the_cap():
    """The qualifying test reads BOTH belief axes: a confident CAUSAL_EVIDENCE
    SUPPORTS link on the hypothesis's chain ROOT is real grounding even with
    an empty flat evidence_links list — a chain-contract emission must not be
    capped and told to link evidence it already linked."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from faultmaven.modules.case.contracts import (
        Case,
        CaseSeverity,
        CaseState,
        CausalNode,
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        EvidenceStance,
        InquiryData,
        NodeEvidenceLink,
        NodeState,
        NodeType,
        ProblemVerification,
        ValidationMethod,
    )

    m = HypothesisManager()
    h = _mk(0.4)
    ev = Evidence(
        evidence_id="ev_" + "b" * 12,
        summary="config diff shows pool max_size dropped to 5",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )
    root = CausalNode(
        node_id="cn_" + "b" * 12,
        statement="undersized pool exhausts under load",
        node_type=NodeType.ROOT,
        node_state=NodeState.INCONCLUSIVE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=ev.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the root",
                linked_at_turn=2,
                stance_confidence=0.9,
            )
        ],
        generated_at_turn=1,
    )
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="X fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="X fails", severity=CaseSeverity.HIGH
        ),
    )
    case.causal_nodes = {root.node_id: root}
    case.evidence = [ev]
    h.root_node_id = root.node_id

    m.update_hypothesis_likelihood(h, 0.8, current_turn=3, reason="LLM", case=case)
    assert h.likelihood == 0.8  # cap lifted by the chain-axis grounding

    # Control: hedged chain link does not lift it.
    root.evidence_links[0].stance_confidence = 0.4
    h2 = _mk(0.4)
    h2.root_node_id = root.node_id
    m.update_hypothesis_likelihood(h2, 0.8, current_turn=3, reason="LLM", case=case)
    assert h2.likelihood == NEW_HYPOTHESIS_MAX_PRIOR
